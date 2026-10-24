"""Deterministic lifecycle regressions; no real providers."""
import json
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from runtime.session.daemon import _DaemonContext, _QueuedRequest, _handle_connection, _worker_loop
from runtime.session.state import SessionState


class SessionHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.state = SessionState('test', 'pi', repo_root=self.root)
        self.ctx = _DaemonContext()

    def request(self, **payload):
        left, right = socket.socketpair()
        try:
            right.sendall(json.dumps(payload).encode() + b'\n')
            _handle_connection(left, self.ctx, self.state, self.root)
            return json.loads(right.recv(65536))
        finally:
            left.close()
            right.close()

    def submit(self, prompt='hello', **kwargs):
        return self.request(action='send', prompt=prompt, nowait=True, **kwargs)

    def test_same_key_is_one_request_and_conflicting_payload_is_rejected(self):
        first = self.submit(request_key='ticket-1', ticket_ref='https://github.com/ShredFred/mco/issues/1')
        again = self.submit(request_key='ticket-1', ticket_ref='https://github.com/ShredFred/mco/issues/1')
        self.assertEqual(first['request_id'], again['request_id'])
        self.assertEqual(self.ctx.request_queue.qsize(), 1)
        conflict = self.submit('changed', request_key='ticket-1')
        self.assertEqual(conflict['code'], 'request_conflict')

    def test_queued_result_is_pending(self):
        ack = self.submit()
        result = self.request(action='result', request_id=ack['request_id'])
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['lifecycle'], 'queued')

    def test_cancel_queued_only_retains_result(self):
        ack = self.submit()
        cancelled = self.request(action='cancel')
        self.assertEqual(cancelled['cancelled'], 1)
        result = self.request(action='result', request_id=ack['request_id'])
        self.assertEqual(result['status'], 'cancelled')

    def test_scoped_cancel_leaves_other_requests(self):
        a = self.submit()
        b = self.submit('other')
        self.request(action='cancel', request_id=b['request_id'])
        self.assertEqual(self.request(action='result', request_id=a['request_id'])['status'], 'pending')
        self.assertEqual(self.request(action='result', request_id=b['request_id'])['status'], 'cancelled')

    def test_dispatch_exception_finishes_waiter_and_worker_continues(self):
        a = self.submit()
        b = self.submit('next')
        original = self.ctx.request_queue.queue[0]
        def dispatch(*args, **kwargs):
            if dispatch.calls == 0:
                dispatch.calls += 1
                raise RuntimeError('injected run failure')
            self.ctx.running = False
            return {'success': True, 'response': 'second worked'}
        dispatch.calls = 0
        with patch('runtime.session.daemon._dispatch_prompt', side_effect=dispatch):
            _worker_loop(self.ctx, self.state, self.root)
        self.assertTrue(original.done.is_set())
        self.assertEqual(self.request(action='result', request_id=a['request_id'])['status'], 'unknown')
        self.assertEqual(self.request(action='result', request_id=b['request_id'])['response'], 'second worked')

class DurableRecoveryTests(unittest.TestCase):
    def test_restart_preserves_ids_results_and_never_replays_running(self):
        from runtime.session.requests import RequestStore
        with tempfile.TemporaryDirectory() as root:
            store = RequestStore(root, 'recover')
            a, _ = store.accept({'prompt': 'a', 'request_key': 'a'})
            b, _ = store.accept({'prompt': 'b', 'request_key': 'b'})
            store.update(a['request_id'], lifecycle='running', status='pending', attempt_id='attempt-a')
            again = RequestStore(root, 'recover')
            queued = again.recover()
            self.assertEqual([i for i, _ in queued], [b['request_id']])
            self.assertEqual(again.get(a['request_id'])['status'], 'unknown')
            receipt, created = again.accept({'prompt': 'a', 'request_key': 'a'})
            self.assertFalse(created)
            self.assertEqual(receipt['request_id'], a['request_id'])
            c, _ = again.accept({'prompt': 'c'})
            self.assertGreater(c['request_id'], b['request_id'])

    def test_concurrent_same_key_has_one_accepted_request(self):
        from concurrent.futures import ThreadPoolExecutor
        from runtime.session.requests import RequestStore
        with tempfile.TemporaryDirectory() as root:
            store = RequestStore(root, 'race')
            with ThreadPoolExecutor(max_workers=8) as pool:
                receipts = list(pool.map(lambda _: store.accept({'prompt': 'p', 'request_key': 'key'}), range(16)))
            self.assertEqual(sum(created for _, created in receipts), 1)
            self.assertEqual(len({r['request_id'] for r, _ in receipts}), 1)

    def test_stop_does_not_signal_an_unverified_pid(self):
        from runtime.session.state import save_state
        from runtime.session.manager import stop_session
        with tempfile.TemporaryDirectory() as root:
            save_state(root, SessionState('stale', 'pi', pid=99999, repo_root=root))
            with patch('runtime.session.client.stop_session', return_value={'status': 'error'}), patch('runtime.session.manager._is_pid_alive', return_value=True), patch('runtime.session.manager.os.kill') as kill:
                self.assertFalse(stop_session(root, 'stale'))
                kill.assert_not_called()

class LifecycleSurfaceTests(unittest.TestCase):
    def test_mcp_submission_uses_shared_client_and_preserves_receipt(self):
        from runtime.mcp_server import _sync_session_submit
        receipt = {'status': 'queued', 'request_id': 7, 'request_key': 'key', 'ticket_ref': 'fork#7'}
        with tempfile.TemporaryDirectory() as root, patch('runtime.session.client.send_prompt_nowait', return_value=receipt) as submit:
            result = _sync_session_submit(root, 'test', 'prompt', 'key', 'fork#7', 'write')
        self.assertEqual(result['data'], receipt)
        self.assertEqual(submit.call_args.kwargs['execution_mode'], 'write')

    def test_json_send_failure_has_nonzero_exit(self):
        from runtime.cli import main
        from contextlib import redirect_stdout
        from io import StringIO
        with patch('runtime.session.client.send_prompt_nowait', return_value={'status': 'error', 'message': 'full'}), redirect_stdout(StringIO()):
            self.assertEqual(main(['session', 'send', 'test', 'prompt', '--no-wait', '--json']), 2)

    def test_mismatched_attempt_cannot_cancel_current_or_queued(self):
        from runtime.session.daemon import _cancel_requests
        ctx = _DaemonContext()
        ctx.current_request = _QueuedRequest(1, 'first', attempt_id='attempt-new')
        ctx.request_queue.put(_QueuedRequest(2, 'second'))
        result = _cancel_requests(ctx, 1, 'attempt-old')
        self.assertEqual(result['code'], 'attempt_mismatch')
        self.assertFalse(ctx.cancel_event.is_set())
        self.assertEqual(ctx.request_queue.qsize(), 1)

class WriteOwnershipTests(unittest.TestCase):
    def test_conflicting_writer_rejected_and_normal_completion_releases(self):
        from runtime.write_ownership import CheckoutWriter, WriteConflict
        with tempfile.TemporaryDirectory() as root:
            first = CheckoutWriter(root, 'first', 'fork#1')
            first.acquire()
            with self.assertRaises(WriteConflict):
                CheckoutWriter(root, 'second').acquire()
            first.release()
            second = CheckoutWriter(root, 'second')
            second.acquire()
            second.release()

    def test_unknown_writer_blocks_takeover_even_for_same_owner(self):
        from runtime.write_ownership import CheckoutWriter, WriteConflict
        with tempfile.TemporaryDirectory() as root:
            CheckoutWriter(root, 'owner').acquire()
            with self.assertRaises(WriteConflict):
                CheckoutWriter(root, 'owner').acquire()

class ClientReceiptTests(unittest.TestCase):
    def test_lost_result_retains_ack_identity(self):
        from runtime.session.client import send_prompt
        with patch('runtime.session.client.socket.socket'), patch('runtime.session.client._LineReader') as reader:
            reader.return_value.read_one.side_effect = [{'status': 'queued', 'request_id': 42}, None]
            receipt = send_prompt('.', 'test', 'prompt', request_key='stable')
        self.assertEqual(receipt['status'], 'unknown')
        self.assertEqual(receipt['request_id'], 42)
        self.assertEqual(receipt['request_key'], 'stable')

    def test_interrupt_cancels_only_its_key_even_before_ack(self):
        from runtime.session.client import send_prompt
        with patch('runtime.session.client.socket.socket'), patch('runtime.session.client._LineReader') as reader, patch('runtime.session.client._send_request') as control:
            reader.return_value.read_one.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                send_prompt('.', 'test', 'prompt', request_key='mine')
        self.assertEqual(control.call_args.args[1], {'action': 'cancel', 'request_key': 'mine'})


class SocketRecoveryTests(unittest.TestCase):
    def test_completed_result_and_idempotency_survive_real_daemon_restart(self):
        import time
        from runtime.session.daemon import run_daemon
        from runtime.session.state import save_state
        from runtime.session.client import ping_session, send_prompt_nowait, get_result, stop_session
        with tempfile.TemporaryDirectory() as root, patch('runtime.session.daemon._dispatch_prompt', return_value={'success': True, 'response': 'done'}) as dispatch:
            save_state(root, SessionState('restart', 'pi', repo_root=root))
            def start():
                thread = threading.Thread(target=run_daemon, args=(root, 'restart'), daemon=True)
                thread.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if ping_session(root, 'restart'):
                        return thread
                    time.sleep(0.02)
                self.fail('daemon failed to become ready')
            thread = start()
            try:
                ack = send_prompt_nowait(root, 'restart', 'prompt', request_key='same')
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    result = get_result(root, 'restart', ack['request_id'])
                    if result.get('status') == 'ok':
                        break
                    time.sleep(0.02)
                self.assertEqual(result['response'], 'done')
            finally:
                stop_session(root, 'restart')
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(get_result(root, 'restart', ack['request_id'])['response'], 'done')
            thread = start()
            try:
                replay = send_prompt_nowait(root, 'restart', 'prompt', request_key='same')
                self.assertEqual(replay['request_id'], ack['request_id'])
                self.assertEqual(replay['status'], 'ok')
                self.assertEqual(dispatch.call_count, 1)
            finally:
                stop_session(root, 'restart')
                thread.join(5)

class RetryAndGuardTests(unittest.TestCase):
    def test_write_failure_is_not_automatically_retried(self):
        from types import SimpleNamespace
        from runtime.session.daemon import _dispatch_prompt
        adapter = SimpleNamespace(detect=lambda: SimpleNamespace(detected=True, auth_ok=True))
        with tempfile.TemporaryDirectory() as root, patch('runtime.cli._doctor_adapter_registry', return_value={'pi': adapter}), patch('runtime.session.daemon._run_single_attempt', return_value={'success': False, 'error_kind': 'retryable_timeout'}) as attempt:
            result = _dispatch_prompt('pi', root, 'write', options={'execution_mode': 'write', 'artifact_root': root})
        self.assertEqual(attempt.call_count, 1)
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(attempt.call_args.args[1].metadata['provider_permissions'], {'tool_profile': 'write'})

    def test_guarded_workflow_serializes_and_preserves_ticket(self):
        from runtime.write_ownership import guard_workflow, CheckoutWriter, WriteConflict
        seen = []
        @guard_workflow
        def fake(**kwargs):
            seen.append(kwargs['max_provider_parallelism'])
            return {'outputs': [{'status': 'success', 'exit_code': 0}], 'artifact_root': None}
        with tempfile.TemporaryDirectory() as root:
            result = fake(repo_root=root, write_owner='owner', ticket_ref='fork#7')
            self.assertEqual(seen, [1])
            self.assertEqual(result['ticket_ref'], 'fork#7')
            held = CheckoutWriter(root, 'other')
            held.acquire()
            with self.assertRaises(WriteConflict):
                fake(repo_root=root, write_owner='owner')
            self.assertEqual(seen, [1])
            held.release()

    def test_daemon_lock_cannot_be_taken_twice(self):
        from pathlib import Path
        from runtime.session.locking import daemon_lock
        with tempfile.TemporaryDirectory() as root:
            with daemon_lock(Path(root) / 'daemon.lock') as first:
                with daemon_lock(Path(root) / 'daemon.lock') as second:
                    self.assertTrue(first)
                    self.assertFalse(second)


if __name__ == '__main__':
    unittest.main()
