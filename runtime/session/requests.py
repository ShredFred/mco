"""Durable accepted requests. SQLite transactions commit before socket receipts."""
from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
from typing import Any, Dict, Optional

from .state import session_dir


class RequestStore:
    def __init__(self, repo_root: str, name: str) -> None:
        self.path = session_dir(repo_root, name) / 'requests.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_key TEXT UNIQUE,
                payload TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                receipt TEXT NOT NULL
            )''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def accept(self, payload: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
        key = payload.get('request_key') or None
        encoded = json.dumps(payload, sort_keys=True)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if key:
                row = db.execute('SELECT payload, receipt FROM requests WHERE request_key=?', (key,)).fetchone()
                if row:
                    if row[0] != encoded:
                        raise ValueError('request_conflict: request key already has a different payload')
                    return json.loads(row[1]), False
            cursor = db.execute('INSERT INTO requests(request_key,payload,lifecycle,receipt) VALUES(?,?,?,?)',
                                (key, encoded, 'queued', '{}'))
            receipt = {'schema_version': 1, 'request_id': cursor.lastrowid,
                       'request_key': key, 'ticket_ref': payload.get('ticket_ref', ''),
                       'status': 'queued', 'lifecycle': 'queued'}
            db.execute('UPDATE requests SET receipt=? WHERE id=?', (json.dumps(receipt), cursor.lastrowid))
            return receipt, True

    def get(self, request_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as db:
            row = db.execute('SELECT receipt FROM requests WHERE id=?', (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def update(self, request_id: int, **fields: Any) -> Dict[str, Any]:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT receipt FROM requests WHERE id=?', (request_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown request {}'.format(request_id))
            receipt = json.loads(row[0])
            receipt.update(fields)
            db.execute('UPDATE requests SET lifecycle=?,receipt=? WHERE id=?',
                       (receipt['lifecycle'], json.dumps(receipt), request_id))
        return receipt

    def recover(self) -> list[tuple[int, Dict[str, Any]]]:
        """Never replay a possibly started write, even if its result was lost."""
        with self.connect() as db:
            rows = db.execute("SELECT id,payload,receipt,lifecycle FROM requests WHERE lifecycle IN ('queued','running') ORDER BY id").fetchall()
            queued = []
            for request_id, payload, raw, lifecycle in rows:
                if lifecycle == 'queued':
                    queued.append((request_id, json.loads(payload)))
                else:
                    receipt = json.loads(raw)
                    receipt.update(status='unknown', lifecycle='unknown',
                                   message='Daemon interrupted; execution outcome requires reconciliation')
                    db.execute('UPDATE requests SET lifecycle=?,receipt=? WHERE id=?',
                               ('unknown', json.dumps(receipt), request_id))
        return queued


def result_receipt(receipt: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(receipt)
    if result['lifecycle'] in ('queued', 'running'):
        result['status'] = 'pending'
    return result
