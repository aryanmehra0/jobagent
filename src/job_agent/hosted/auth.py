"""Per-user bearer keys and isolated hosted job records on SQLite or Postgres."""
from __future__ import annotations
import hashlib
import json
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone

from job_agent.hosted.queue import HostedQueue


class HostedIdentityStore:
    def __init__(self, queue=None):
        self.queue = queue or HostedQueue()
        with self.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS hosted_users (user_id TEXT PRIMARY KEY, created_at TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS hosted_api_keys (key_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES hosted_users(user_id), key_hash TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS hosted_user_jobs (user_id TEXT NOT NULL REFERENCES hosted_users(user_id), job_id TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(user_id, job_id))')

    @contextmanager
    def connect(self):
        with (self.queue._pg_connect() if self.queue.database_url else self.queue._connect()) as conn:
            yield conn

    def execute(self, conn, sql, values=()):
        return conn.execute(sql.replace('?', '%s') if self.queue.database_url else sql, values)

    def issue_key(self, user_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', user_id):
            raise ValueError('User ID must contain only letters, digits, hyphens or underscores.')
        now = datetime.now(timezone.utc).isoformat()
        key_id = secrets.token_hex(12)
        token = key_id + '.' + secrets.token_urlsafe(32)
        with self.connect() as conn:
            self.execute(conn, 'INSERT INTO hosted_users(user_id, created_at) VALUES (?, ?) ON CONFLICT(user_id) DO NOTHING', (user_id, now))
            self.execute(conn, 'INSERT INTO hosted_api_keys(key_id, user_id, key_hash, created_at) VALUES (?, ?, ?, ?)',
                         (key_id, user_id, hashlib.sha256(token.encode()).hexdigest(), now))
        return token

    def authenticate(self, token):
        if not re.fullmatch(r'[a-f0-9]{24}\.[A-Za-z0-9_-]{43}', token):
            return None
        with self.connect() as conn:
            row = self.execute(conn, 'SELECT user_id, key_hash, revoked FROM hosted_api_keys WHERE key_id=?', (token.split('.')[0],)).fetchone()
        if row and not row['revoked'] and secrets.compare_digest(row['key_hash'], hashlib.sha256(token.encode()).hexdigest()):
            return row['user_id']
        return None

    def revoke(self, key_id):
        with self.connect() as conn:
            cursor = self.execute(conn, 'UPDATE hosted_api_keys SET revoked=1 WHERE key_id=?', (key_id,))
            return cursor.rowcount > 0

    def put_job(self, user_id, job_id, payload):
        """For a future isolated worker, never imports the shared personal jobs DB."""
        with self.connect() as conn:
            if not self.execute(conn, 'SELECT user_id FROM hosted_users WHERE user_id=?', (user_id,)).fetchone():
                raise ValueError('Unknown hosted user')
            self.execute(conn, 'INSERT INTO hosted_user_jobs(user_id, job_id, payload_json) VALUES (?, ?, ?) ON CONFLICT(user_id, job_id) DO UPDATE SET payload_json=excluded.payload_json',
                         (user_id, job_id, json.dumps(payload)))

    def jobs(self, user_id, limit=50):
        with self.connect() as conn:
            rows = self.execute(conn, 'SELECT payload_json FROM hosted_user_jobs WHERE user_id=? ORDER BY job_id LIMIT ?', (user_id, max(1, min(500, limit)))).fetchall()
        return [json.loads(row['payload_json']) for row in rows]

    def job_count(self, user_id):
        with self.connect() as conn:
            return self.execute(conn, 'SELECT COUNT(*) AS n FROM hosted_user_jobs WHERE user_id=?', (user_id,)).fetchone()['n']
