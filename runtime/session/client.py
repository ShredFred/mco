"""Session client — connect to daemon socket, send prompts, broadcast."""
from __future__ import annotations

import json
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from .state import SessionState, session_dir, load_state


def _socket_path(repo_root: str, name: str) -> str:
    return str(session_dir(repo_root, name) / "sock")


def _send_request(
    sock_path: str,
    request: Dict[str, Any],
    timeout: float = 600.0,
) -> Dict[str, Any]:
    """Send a JSON-line request to a daemon socket and return the response."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(sock_path)
        client.sendall(json.dumps(request).encode("utf-8") + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
        if not data:
            return {"status": "error", "message": "Empty response from daemon"}
        return json.loads(data.decode("utf-8").strip())
    except socket.timeout:
        return {"status": "error", "message": "Timeout waiting for daemon response"}
    except ConnectionRefusedError:
        return {"status": "error", "message": "Cannot connect to session daemon (socket refused)"}
    except FileNotFoundError:
        return {"status": "error", "message": "Session socket not found — session may not be running"}
    finally:
        client.close()


class _LineReader:
    """Buffered reader that splits on \\n, preserving leftover bytes between reads."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = b""

    def read_one(self) -> Optional[Dict[str, Any]]:
        """Read and parse one JSON line. Returns None on EOF."""
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))


def _submit(repo_root: str, name: str, prompt: str, nowait: bool,
            request_key: str = "", ticket_ref: str = "", execution_mode: str = "read_only") -> Dict[str, Any]:
    from uuid import uuid4
    key = request_key or uuid4().hex
    receipt: Dict[str, Any] = {"request_key": key}
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(10.0 if nowait else 900.0)
    payload = {"action": "send", "prompt": prompt, "nowait": nowait, "request_key": key, "execution_mode": execution_mode}
    if ticket_ref:
        payload["ticket_ref"] = ticket_ref
    try:
        client.connect(_socket_path(repo_root, name))
        client.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        reader = _LineReader(client)
        first = reader.read_one()
        if first is None:
            return dict(receipt, status="unknown", message="Acceptance unknown; resubmit with the same request_key")
        receipt.update(first)
        if nowait or first.get("status") != "queued":
            return receipt
        second = reader.read_one()
        if second is None:
            return dict(receipt, status="unknown", message="Connection lost; query request result")
        return second
    except KeyboardInterrupt:
        # Scope Ctrl+C even when the acknowledgement was lost.
        _send_request(_socket_path(repo_root, name), {"action": "cancel", "request_key": key}, timeout=5.0)
        raise
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        return dict(receipt, status="error", message=str(exc))
    except (OSError, ValueError) as exc:
        return dict(receipt, status="unknown", message=str(exc))
    finally:
        client.close()


def send_prompt(repo_root: str, name: str, prompt: str,
                request_key: str = "", ticket_ref: str = "", execution_mode: str = "read_only") -> Dict[str, Any]:
    """Wait for a result; preserve identity if acknowledgement/result is lost."""
    return _submit(repo_root, name, prompt, False, request_key, ticket_ref, execution_mode)


def send_prompt_nowait(repo_root: str, name: str, prompt: str,
                       request_key: str = "", ticket_ref: str = "", execution_mode: str = "read_only") -> Dict[str, Any]:
    """Return a durable acceptance receipt; same key retries do not requeue."""
    return _submit(repo_root, name, prompt, True, request_key, ticket_ref, execution_mode)


def ping_session(repo_root: str, name: str) -> bool:
    """Check if a session daemon is alive."""
    sock_path = _socket_path(repo_root, name)
    result = _send_request(sock_path, {"action": "ping"}, timeout=5.0)
    state = load_state(repo_root, name)
    return bool(state and state.daemon_id and result.get("status") == "pong"
                and result.get("daemon_id") == state.daemon_id and result.get("pid") == state.pid)


def stop_session(repo_root: str, name: str, daemon_id: str = "") -> Dict[str, Any]:
    """Send shutdown to a session daemon."""
    sock_path = _socket_path(repo_root, name)
    payload = {"action": "shutdown"}
    if daemon_id:
        payload["daemon_id"] = daemon_id
    return _send_request(sock_path, payload, timeout=10.0)


def cancel_session(repo_root: str, name: str, request_id: Optional[int] = None,
                   attempt_id: str = "") -> Dict[str, Any]:
    """Send cancel to a session daemon. Cancels running + queued requests."""
    sock_path = _socket_path(repo_root, name)
    payload: Dict[str, Any] = {"action": "cancel"}
    if request_id is not None:
        payload["request_id"] = request_id
    if attempt_id:
        if request_id is None:
            return {"status": "error", "message": "attempt_id requires request_id"}
        payload["attempt_id"] = attempt_id
    return _send_request(sock_path, payload, timeout=10.0)


def queue_status(repo_root: str, name: str) -> Dict[str, Any]:
    """Query queue status of a session daemon."""
    sock_path = _socket_path(repo_root, name)
    return _send_request(sock_path, {"action": "queue"}, timeout=5.0)


def get_result(repo_root: str, name: str, request_id: int) -> Dict[str, Any]:
    """Retrieve the result of a previously submitted nowait request.

    Returns the result if the request has completed, or {status: "pending"}.
    """
    from .requests import RequestStore, result_receipt
    path = session_dir(repo_root, name) / "requests.sqlite3"
    if path.exists():
        receipt = RequestStore(repo_root, name).get(request_id)
        if receipt is not None:
            result = result_receipt(receipt)
            if result["status"] == "pending" and not ping_session(repo_root, name):
                result.update(status="unknown", message="Daemon unavailable; execution outcome is unverified")
            return result
        return {"status": "error", "code": "unknown_request", "request_id": request_id,
                "message": "Request {} not found".format(request_id)}
    return _send_request(_socket_path(repo_root, name), {"action": "result", "request_id": request_id}, timeout=5.0)


def broadcast_prompt(
    repo_root: str,
    prompt: str,
) -> List[Dict[str, Any]]:
    """Send a prompt to ALL active sessions in parallel.

    Uses manager.list_sessions for PID liveness check (not raw state files).
    Returns a list of {session_name, provider, status, response, wall_clock_seconds}.
    """
    from .manager import list_sessions as list_sessions_live
    live_sessions = list_sessions_live(repo_root)
    active = [s for s in live_sessions if s["status"] == "active"]

    if not active:
        return []

    results: List[Dict[str, Any]] = []

    def _send_one(session_info: Dict[str, Any]) -> Dict[str, Any]:
        resp = send_prompt(repo_root, session_info["name"], prompt)
        return {
            "session_name": session_info["name"],
            "provider": session_info["provider"],
            "status": resp.get("status", "error"),
            "response": resp.get("response", ""),
            "wall_clock_seconds": resp.get("wall_clock_seconds", 0),
            "message": resp.get("message", ""),
        }

    with ThreadPoolExecutor(max_workers=len(active)) as executor:
        futures = {executor.submit(_send_one, s): s for s in active}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                session_info = futures[future]
                results.append({
                    "session_name": session_info["name"],
                    "provider": session_info["provider"],
                    "status": "error",
                    "response": "",
                    "wall_clock_seconds": 0,
                    "message": str(exc),
                })

    return results
