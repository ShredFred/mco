"""Session daemon — unix socket listener with request queue and cancellation."""
from __future__ import annotations

import json
import logging
import os
import queue
import signal
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .requests import RequestStore, result_receipt

from .state import (
    SessionState,
    HistoryEntry,
    _now_iso,
    append_history,
    build_history_prompt,
    load_history,
    load_state,
    save_state,
    session_dir,
)


_STALL_TIMEOUT_SECONDS = 900  # Match default provider stall timeout
_MAX_QUEUE_DEPTH = 10


def _socket_path(repo_root: str, name: str) -> str:
    return str(session_dir(repo_root, name) / "sock")


@dataclass
class _QueuedRequest:
    """A prompt waiting to be processed by the worker thread."""
    request_id: int
    prompt: str
    done: threading.Event = field(default_factory=threading.Event)
    result: Optional[Dict[str, Any]] = None
    cancelled: bool = False
    attempt_id: str = ""
    options: Dict[str, Any] = field(default_factory=dict)


_MAX_COMPLETED_RESULTS = 50  # Keep last N completed results for nowait retrieval


class _DaemonContext:
    """Shared mutable state for daemon threads."""

    def __init__(self) -> None:
        self.request_queue: queue.Queue[_QueuedRequest] = queue.Queue(
            maxsize=_MAX_QUEUE_DEPTH,
        )
        self.lock = threading.RLock()
        self.current_request: Optional[_QueuedRequest] = None
        self.cancel_event = threading.Event()
        self.store: Optional[RequestStore] = None
        self.requests: Dict[int, _QueuedRequest] = {}
        self.worker: Optional[threading.Thread] = None
        self.health_error = ""
        self.running = True
        # Store completed results for nowait retrieval
        self.completed_results: Dict[int, Dict[str, Any]] = {}


def _read_output(artifact_path: str, provider: str) -> tuple:
    """Read stdout/stderr from provider artifact logs. Returns (raw_stdout, raw_stderr)."""
    raw_dir = Path(artifact_path) / "raw"
    stdout_path = raw_dir / "{}.stdout.log".format(provider)
    stderr_path = raw_dir / "{}.stderr.log".format(provider)
    raw_stdout = ""
    raw_stderr = ""
    if stdout_path.exists():
        raw_stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    if stderr_path.exists():
        raw_stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    return raw_stdout, raw_stderr


def _extract_response(raw_stdout: str, raw_stderr: str) -> str:
    """Extract human-readable text from raw provider output."""
    from ..output_text import extract_final_text_from_output
    combined = raw_stdout
    if raw_stderr:
        combined = combined + "\n" + raw_stderr if combined else raw_stderr
    return (extract_final_text_from_output(combined) or raw_stdout).strip()


def _run_single_attempt(
    adapter: object,
    task_input: object,
    provider: str,
    cancel_event: Optional[threading.Event],
) -> Dict[str, Any]:
    """Run one attempt of a prompt dispatch. Returns attempt result dict."""
    run_ref = adapter.run(task_input)  # type: ignore[union-attr]
    started = time.time()

    while True:
        if cancel_event and cancel_event.is_set():
            try:
                adapter.cancel(run_ref)  # type: ignore[union-attr]
            except Exception as exc:
                logging.warning(f"Failed to cancel provider {provider}: {exc}")
            raw_stdout, raw_stderr = _read_output(run_ref.artifact_path, provider)
            return {
                "success": False,
                "response": _extract_response(raw_stdout, raw_stderr),
                "error": "Cancelled",
                "error_kind": "cancelled",
                "outcome_unknown": True,
                "wall_clock_seconds": round(time.time() - started, 2),
            }

        status = adapter.poll(run_ref)  # type: ignore[union-attr]
        if status.completed:
            break
        time.sleep(1.0)

        if time.time() - started > _STALL_TIMEOUT_SECONDS:
            try:
                adapter.cancel(run_ref)  # type: ignore[union-attr]
            except Exception as exc:
                logging.warning(f"Failed to cancel provider {provider}: {exc}")
            # Preserve partial output on timeout
            raw_stdout, raw_stderr = _read_output(run_ref.artifact_path, provider)
            return {
                "success": False,
                "response": _extract_response(raw_stdout, raw_stderr),
                "error": "Provider timed out after {}s".format(_STALL_TIMEOUT_SECONDS),
                "error_kind": "retryable_timeout",
                "outcome_unknown": True,
                "wall_clock_seconds": round(time.time() - started, 2),
            }

    wall_clock = round(time.time() - started, 2)
    raw_stdout, raw_stderr = _read_output(run_ref.artifact_path, provider)
    response = _extract_response(raw_stdout, raw_stderr)

    success = status.attempt_state == "SUCCEEDED" and type(status.exit_code) is int and status.exit_code == 0
    if not success:
        # Classify the error for retry decisions
        from ..errors import classify_error
        exit_code = getattr(status, "exit_code", 1)
        error_kind = classify_error(exit_code if isinstance(exit_code, int) else 1, raw_stderr)
        return {
            "success": False,
            "response": response,
            "error": status.message if hasattr(status, "message") else "Provider failed",
            "error_kind": error_kind.value,
            "termination_confirmed": type(status.exit_code) is int,
            "outcome_unknown": type(status.exit_code) is not int,
            "wall_clock_seconds": wall_clock,
        }

    return {
        "success": True,
        "response": response,
        "termination_confirmed": isinstance(status.exit_code, int),
        "wall_clock_seconds": wall_clock,
    }


# Retryable error kinds from the shared error taxonomy.
_RETRYABLE_ERROR_KINDS = {"retryable_timeout", "retryable_rate_limit", "retryable_transient_network"}


def _dispatch_prompt(
    provider: str,
    repo_root: str,
    prompt: str,
    cancel_event: Optional[threading.Event] = None,
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run a single prompt through the provider adapter with retry support.

    Returns {success, response, wall_clock_seconds, [error], [error_kind], [attempts]}.
    Uses error classification for retry decisions and exponential backoff.
    """
    from ..cli import SUPPORTED_PROVIDERS, _doctor_adapter_registry
    from ..contracts import TaskInput
    from ..retry import RetryPolicy

    adapters = _doctor_adapter_registry()
    adapter = adapters.get(provider)
    if adapter is None:
        return {"success": False, "response": "", "termination_confirmed": True, "error": "No adapter for provider: {}".format(provider)}

    presence = adapter.detect()
    if not presence.detected or not presence.auth_ok:
        return {"success": False, "response": "", "termination_confirmed": True, "error": "Provider not available: {}".format(provider)}

    from ..execution_modes import execution_permissions
    options = options or {}
    execution_mode = options.get("execution_mode", "read_only")
    permissions = execution_permissions(provider, execution_mode)
    if permissions is None:
        return {"success": False, "termination_confirmed": True, "error": "Unsupported execution mode for provider"}
    retry_policy = RetryPolicy()
    max_retries = retry_policy.max_retries if execution_mode == "read_only" else 0
    total_started = time.time()
    last_result: Optional[Dict[str, Any]] = None

    import tempfile
    import uuid

    for attempt in range(1, max_retries + 2):  # 1 initial + max_retries
        # Check cancellation before each attempt
        if cancel_event and cancel_event.is_set():
            return {
                "success": False,
                "response": (last_result or {}).get("response", ""),
                "error": "Cancelled",
                "wall_clock_seconds": round(time.time() - total_started, 2),
                "attempts": attempt - 1,
            }

        from contextlib import nullcontext
        artifact_context = (nullcontext(options["artifact_root"]) if options.get("artifact_root")
                            else tempfile.TemporaryDirectory(prefix="mco-session-"))
        with artifact_context as artifact_dir:
            unique_id = "session-{}-{}".format(int(time.time()), uuid.uuid4().hex[:8])
            task_input = TaskInput(
                task_id=unique_id,
                prompt=prompt,
                repo_root=repo_root,
                target_paths=["."],
                timeout_seconds=_STALL_TIMEOUT_SECONDS,
                metadata={"artifact_root": artifact_dir, "provider_permissions": permissions,
                          "request_id": options.get("request_id"),
                          "attempt_id": options.get("attempt_id"),
                          "ticket_ref": options.get("ticket_ref", "")},
            )

            result = _run_single_attempt(adapter, task_input, provider, cancel_event)
            result["attempts"] = attempt
            result["artifact_root"] = str(artifact_dir) if options.get("artifact_root") else None
            last_result = result

            if result["success"]:
                return result

            # Check if error is retryable
            error_kind = result.get("error_kind", "")
            if error_kind not in _RETRYABLE_ERROR_KINDS or attempt > max_retries:
                return result

            # Backoff before retry
            delay = retry_policy.compute_delay(attempt)
            # Sleep in small increments to check cancellation
            deadline = time.time() + delay
            while time.time() < deadline:
                if cancel_event and cancel_event.is_set():
                    result["error"] = "Cancelled during retry backoff"
                    return result
                time.sleep(min(0.5, deadline - time.time()))

    return last_result or {"success": False, "response": "", "error": "All retries exhausted"}


def _worker_loop(
    ctx: _DaemonContext,
    state: SessionState,
    repo_root: str,
) -> None:
    """Worker thread — processes queued requests one at a time."""
    import uuid
    while ctx.running:
        with ctx.lock:
            try:
                req = ctx.request_queue.get_nowait()
            except queue.Empty:
                req = None
            if req is not None:
                ctx.current_request = req
                ctx.cancel_event.clear()
        if req is None:
            time.sleep(0.05)
            continue

        writer = None
        dispatched = False
        try:
            if req.cancelled:
                continue
            req.attempt_id = uuid.uuid4().hex
            if ctx.store:
                ctx.store.update(req.request_id, status="pending", lifecycle="running",
                                 attempt_id=req.attempt_id)
            history = load_history(repo_root, state.name)
            full_prompt = build_history_prompt(history, req.prompt)
            options = dict(req.options, request_id=req.request_id, attempt_id=req.attempt_id,
                           artifact_root=str(session_dir(repo_root, state.name) / "requests" / str(req.request_id) / req.attempt_id))
            if req.options.get("execution_mode", "read_only") != "read_only":
                from ..write_ownership import CheckoutWriter
                writer = CheckoutWriter(repo_root, "{}:{}:{}".format(state.name, req.request_id, req.attempt_id),
                                        req.options.get("ticket_ref", ""))
                writer.acquire()
            dispatched = True
            result = _dispatch_prompt(state.provider, repo_root, full_prompt, ctx.cancel_event, options)
            if writer and result.get("termination_confirmed", False):
                writer.release()
                writer = None
            success = result.get("success", False)
            cancelled = ctx.cancel_event.is_set()
            if success and result.get("response") and not cancelled:
                append_history(repo_root, state.name, HistoryEntry(role="user", content=req.prompt))
                append_history(repo_root, state.name, HistoryEntry(
                    role="assistant", content=result["response"],
                    wall_clock_seconds=result.get("wall_clock_seconds", 0)))
                state.turn_count += 1
            state.last_active = _now_iso()
            save_state(repo_root, state)
            response = {
                "status": "ok" if success else "error",
                "lifecycle": "succeeded" if success else "failed",
                "response": result.get("response", ""),
                "wall_clock_seconds": result.get("wall_clock_seconds", 0),
                "attempt_id": req.attempt_id,
                "attempts": result.get("attempts", 1),
                "artifact_root": result.get("artifact_root"),
            }
            if result.get("outcome_unknown"):
                response.update(status="unknown", lifecycle="unknown", termination_confirmed=False)
            if cancelled:
                # The adapter cancel API does not prove child termination.
                response.update(status="cancelled", lifecycle="unknown",
                                cancellation_requested=True, termination_confirmed=False,
                                message="Cancellation requested; verify provider termination before retry")
            elif not success and result.get("error"):
                response["message"] = result["error"]
            _finish_request(ctx, req, response)
        except Exception as exc:
            logging.exception("Session request %s failed", req.request_id)
            try:
                _finish_request(ctx, req, {
                    "status": "unknown" if dispatched else "error",
                    "lifecycle": "unknown" if dispatched else "failed", "message": str(exc),
                    "code": "execution_unknown" if dispatched else "pre_dispatch_failure",
                    "attempt_id": req.attempt_id,
                })
            except Exception as persist_exc:
                ctx.health_error = str(persist_exc)
                req.result = {"status": "unknown", "request_id": req.request_id,
                              "message": "Cannot persist outcome: {}".format(persist_exc)}
                req.done.set()
        finally:
            with ctx.lock:
                ctx.current_request = None
                ctx.requests.pop(req.request_id, None)


def _finish_request(ctx: _DaemonContext, req: _QueuedRequest, response: Dict[str, Any]) -> None:
    response = dict(response, request_id=req.request_id)
    if ctx.store:
        response = ctx.store.update(req.request_id, **{k: v for k, v in response.items() if k != "request_id"})
    req.result = response
    with ctx.lock:
        ctx.completed_results[req.request_id] = response
        if len(ctx.completed_results) > _MAX_COMPLETED_RESULTS:
            del ctx.completed_results[min(ctx.completed_results)]
    req.done.set()


def _cancel_requests(ctx: _DaemonContext, request_id: Optional[int] = None,
                     attempt_id: Optional[str] = None) -> Dict[str, Any]:
    with ctx.lock:
        current = ctx.current_request
        if attempt_id and (current is None or current.request_id != request_id or current.attempt_id != attempt_id):
            return {"status": "error", "code": "attempt_mismatch", "cancelled": 0,
                    "message": "Cancellation does not match the current attempt"}
        count = 0
        if current is not None and (request_id is None or current.request_id == request_id):
            ctx.cancel_event.set()
            count += 1
        retained = []
        while True:
            try:
                queued = ctx.request_queue.get_nowait()
            except queue.Empty:
                break
            if request_id is not None and queued.request_id != request_id:
                retained.append(queued)
                continue
            queued.cancelled = True
            _finish_request(ctx, queued, {"status": "cancelled", "lifecycle": "cancelled",
                "response": "", "message": "Cancelled before execution", "wall_clock_seconds": 0,
                "termination_confirmed": True})
            ctx.requests.pop(queued.request_id, None)
            count += 1
        for queued in retained:
            ctx.request_queue.put_nowait(queued)
        return {"status": "ok", "request_id": request_id, "cancelled": count,
                "cancellation_requested": bool(count)}


_MAX_REQUEST_SIZE = 1024 * 1024  # 1 MB


def _read_request(conn: socket.socket) -> Optional[Dict[str, Any]]:
    """Read a single JSON-line request from the connection."""
    data = b""
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > _MAX_REQUEST_SIZE:
            raise ValueError("Request exceeds maximum size of {} bytes".format(_MAX_REQUEST_SIZE))
        if b"\n" in data:
            break
    if not data:
        return None
    return json.loads(data.decode("utf-8").strip())


def _send_response(conn: socket.socket, response: Dict[str, Any]) -> None:
    """Send a JSON-line response on the connection."""
    conn.sendall(json.dumps(response).encode("utf-8") + b"\n")


def _handle_connection(
    conn: socket.socket,
    ctx: _DaemonContext,
    state: SessionState,
    repo_root: str,
) -> bool:
    """Handle one client connection. Returns False if shutdown requested."""
    try:
        request = _read_request(conn)
        if request is None:
            return True

        action = request.get("action", "")
        expected = request.get("daemon_id")
        if expected is not None and expected != state.daemon_id:
            _send_response(conn, {"status": "error", "code": "daemon_mismatch", "message": "Daemon incarnation changed"})
            return True
        with ctx.lock:
            if ctx.store is None:
                ctx.store = RequestStore(repo_root, state.name)

        if action == "ping":
            healthy = not ctx.health_error and (ctx.worker is None or ctx.worker.is_alive())
            _send_response(conn, {"status": "pong" if healthy else "unhealthy",
                                  "daemon_id": state.daemon_id, "pid": state.pid})
            return True

        if action == "shutdown":
            with ctx.lock:
                ctx.running = False
                _cancel_requests(ctx)
            _send_response(conn, {"status": "shutdown_ack"})
            return False

        if action == "send":
            prompt = request.get("prompt", "")
            if not isinstance(prompt, str) or not prompt.strip():
                _send_response(conn, {"status": "error", "message": "Empty prompt or invalid type"})
                return True
            for key in ("request_key", "ticket_ref"):
                if key in request and (not isinstance(request[key], str) or not request[key].strip()):
                    _send_response(conn, {"status": "error", "message": "Invalid {}".format(key)})
                    return True
            execution_mode = request.get("execution_mode", "read_only")
            if execution_mode not in ("read_only", "write", "yolo"):
                _send_response(conn, {"status": "error", "message": "Invalid execution_mode"})
                return True
            payload = {"prompt": prompt, "request_key": request.get("request_key", ""),
                       "ticket_ref": request.get("ticket_ref", ""), "execution_mode": execution_mode}
            with ctx.lock:
                if not ctx.running or ctx.health_error:
                    _send_response(conn, {"status": "error", "code": "unavailable",
                                          "message": "Session stopping or unhealthy"})
                    return True
                # Duplicate receipts remain queryable even when the queue is full.
                if ctx.request_queue.full():
                    with ctx.store.connect() as db:
                        exists = db.execute('SELECT id FROM requests WHERE request_key=?',
                                            (payload["request_key"] or None,)).fetchone()
                    if not exists:
                        _send_response(conn, {"status": "error", "message": "Queue full"})
                        return True
                try:
                    receipt, created = ctx.store.accept(payload)
                except ValueError as exc:
                    _send_response(conn, {"status": "error", "code": "request_conflict", "message": str(exc)})
                    return True
                request_id = receipt["request_id"]
                if created:
                    req = _QueuedRequest(request_id=request_id, prompt=prompt, options=payload)
                    ctx.requests[request_id] = req
                    ctx.request_queue.put_nowait(req)
                else:
                    req = ctx.requests.get(request_id)
                ack = dict(receipt, position=ctx.request_queue.qsize())
                if receipt["lifecycle"] in ("queued", "running"):
                    ack["status"] = "queued"
            _send_response(conn, ack)
            if request.get("nowait", False) or req is None or receipt["lifecycle"] not in ("queued", "running"):
                return True
            req.done.wait()
            _send_response(conn, req.result or {"status": "unknown", "request_id": request_id})
            return True

        if action == "cancel":
            req_id = request.get("request_id")
            if "request_key" in request:
                with ctx.store.connect() as db:
                    row = db.execute('SELECT id FROM requests WHERE request_key=?',
                                     (request["request_key"],)).fetchone()
                if row is None:
                    _send_response(conn, {"status": "error", "code": "unknown_request", "cancelled": 0})
                    return True
                req_id = row[0]
            if req_id is not None and (type(req_id) is not int or req_id <= 0):
                _send_response(conn, {"status": "error", "message": "Invalid request_id"})
                return True
            _send_response(conn, _cancel_requests(ctx, req_id, request.get("attempt_id")))
            return True

        if action == "queue":
            with ctx.lock:
                running_id = ctx.current_request.request_id if ctx.current_request else None
                queue_size = ctx.request_queue.qsize()

            _send_response(conn, {
                "status": "ok",
                "running": running_id,
                "queued": queue_size,
            })
            return True

        if action == "result":
            req_id = request.get("request_id")
            if type(req_id) is not int or req_id <= 0:
                _send_response(conn, {"status": "error", "message": "Invalid request_id"})
                return True
            stored = ctx.store.get(req_id)
            _send_response(conn, result_receipt(stored) if stored else {
                "status": "error", "code": "unknown_request", "request_id": req_id,
                "message": "Request {} not found".format(req_id)})
            return True

        _send_response(conn, {"status": "error", "message": "Unknown action: {}".format(action)})
        return True

    except Exception as exc:
        try:
            _send_response(conn, {"status": "error", "message": str(exc)})
        except Exception:
            pass
        return True


def _connection_handler(
    conn: socket.socket,
    ctx: _DaemonContext,
    state: SessionState,
    repo_root: str,
) -> None:
    """Threaded connection handler — wraps _handle_connection with cleanup."""
    try:
        should_continue = _handle_connection(conn, ctx, state, repo_root)
        if not should_continue:
            ctx.running = False
    finally:
        conn.close()


def run_daemon(repo_root: str, name: str) -> None:
    from .locking import daemon_lock
    with daemon_lock(session_dir(repo_root, name) / "daemon.lock") as acquired:
        if not acquired:
            logging.error("Session daemon already owns %s", name)
            return
        _serve_daemon(repo_root, name)


def _serve_daemon(repo_root: str, name: str) -> None:
    """Main daemon loop — listen on unix socket, handle requests.

    Uses a worker thread for serial prompt execution and handler threads
    for concurrent connection handling (cancel/queue/ping while busy).
    """
    state = load_state(repo_root, name)
    if state is None:
        return

    sock_path = _socket_path(repo_root, name)

    # Clean up stale socket
    if os.path.exists(sock_path):
        os.unlink(sock_path)

    ctx = _DaemonContext()
    ctx.store = RequestStore(repo_root, name)
    for request_id, payload in ctx.store.recover():
        req = _QueuedRequest(request_id=request_id, prompt=payload["prompt"], options=payload)
        ctx.requests[request_id] = req
        ctx.request_queue.put_nowait(req)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(sock_path)
    except OSError as exc:
        # Socket path too long or permission denied — mark crashed
        state.status = "crashed"
        state.pid = None
        save_state(repo_root, state)
        import sys
        print("Daemon bind failed: {}".format(exc), file=sys.stderr)
        return
    server.listen(5)
    server.settimeout(1.0)

    # Update state with PID — only after successful bind
    import uuid
    state.daemon_id = uuid.uuid4().hex
    state.pid = os.getpid()
    state.status = "active"
    save_state(repo_root, state)

    # Start worker thread
    worker = threading.Thread(
        target=_worker_loop, args=(ctx, state, repo_root), daemon=True,
    )
    ctx.worker = worker
    worker.start()

    def _sigterm_handler(signum: int, frame: Any) -> None:
        ctx.running = False

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _sigterm_handler)

    handler_threads: List[threading.Thread] = []

    try:
        while ctx.running:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            t = threading.Thread(
                target=_connection_handler,
                args=(conn, ctx, state, repo_root),
                daemon=True,
            )
            t.start()
            handler_threads.append(t)

            # Prune finished handler threads periodically
            handler_threads = [t for t in handler_threads if t.is_alive()]
    finally:
        ctx.running = False

        _cancel_requests(ctx)

        # Wait for handler threads (they'll unblock once queued requests resolve)
        for ht in handler_threads:
            ht.join(timeout=3)

        worker.join(timeout=5)
        server.close()
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        state.status = "unknown" if worker.is_alive() else "stopped"
        if not worker.is_alive():
            state.pid = None
        save_state(repo_root, state)
