"""Conservative local write lease. Unknown outcomes require external reconciliation."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
from uuid import uuid4


class WriteConflict(RuntimeError):
    pass


class CheckoutWriter:
    def __init__(self, repo_root: str, owner: str, ticket_ref: str = '') -> None:
        self.root = Path(repo_root).resolve()
        probe = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=str(self.root),
                               capture_output=True, text=True, check=False)
        if probe.returncode == 0:
            self.root = Path(probe.stdout.strip()).resolve()
        base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=str(self.root),
                              capture_output=True, text=True, check=False)
        self.base_commit = base.stdout.strip() if base.returncode == 0 else None
        self.path = self.root / '.mco' / 'write-ownership.sqlite3'
        self.owner = owner
        self.ticket_ref = ticket_ref
        self.token = uuid4().hex

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=10)
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS writer (singleton INTEGER PRIMARY KEY, token TEXT, receipt TEXT)')
                receipt = json.dumps({'owner': self.owner, 'ticket_ref': self.ticket_ref,
                                      'checkout': str(self.root), 'base_commit': self.base_commit, 'token': self.token})
                try:
                    db.execute('INSERT INTO writer VALUES (1,?,?)', (self.token, receipt))
                except sqlite3.IntegrityError:
                    active = db.execute('SELECT receipt FROM writer WHERE singleton=1').fetchone()
                    raise WriteConflict('write_conflict: reconcile existing checkout writer {}'.format(active[0])) from None
        finally:
            db.close()

    def release(self) -> None:
        db = sqlite3.connect(str(self.path), timeout=10)
        try:
            with db:
                db.execute('DELETE FROM writer WHERE singleton=1 AND token=?', (self.token,))
        finally:
            db.close()


def guard_workflow(function):
    """Opt-in lease for legacy workflows; serialize providers inside the owner."""
    from functools import wraps

    @wraps(function)
    def guarded(**kwargs):
        owner = kwargs.get('write_owner', '')
        writer = None
        if owner:
            writer = CheckoutWriter(kwargs['repo_root'], owner, kwargs.get('ticket_ref', ''))
            writer.acquire()
            # A lease protects the checkout, not individual files within it.
            kwargs['max_provider_parallelism'] = 1
        result = function(**kwargs)
        if writer:
            outputs = result.get('outputs', [])
            if outputs and all(type(item.get('exit_code')) is int
                               and item.get('status') not in ('cancelled', 'timeout') for item in outputs):
                writer.release()
            result['write_owner'] = owner
            result['write_lease'] = str(writer.path)
        if owner or kwargs.get('ticket_ref'):
            result['ticket_ref'] = kwargs.get('ticket_ref', '')
            artifact_root = result.get('artifact_root')
            if artifact_root:
                record = Path(str(artifact_root)) / 'run.json'
                if record.exists():
                    payload = json.loads(record.read_text(encoding='utf-8'))
                    payload.update(write_owner=owner, ticket_ref=kwargs.get('ticket_ref', ''))
                    record.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
        return result

    return guarded
