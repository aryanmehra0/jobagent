"""Remember job embeddings between runs.

Encoding a posting costs far more than looking its vector up, and the same postings are scored
again whenever a run is repeated, resumed, or re-scored after a setting changed. A job's vector
depends only on the model and the text, never on the candidate, so it stays valid across
profile switches and is safe to keep. The key is a hash of both, so a different model or an
edited description misses the cache instead of returning a stale vector.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import numpy as np

from job_agent.config.settings import settings

CACHE_FILE = "embedding_cache.db"
MAX_ROWS = 20000


class EmbeddingCache:
    def __init__(self, model_name: str, path: Optional[Path] = None):
        self.model_name = model_name
        self.path = Path(path or settings.outputs_dir / CACHE_FILE)
        self.hits = 0
        self.misses = 0

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model_name}\x00{text}".encode("utf-8")).hexdigest()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=15.0)
        conn.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, dim INTEGER NOT NULL, "
                     "vec BLOB NOT NULL, used_at REAL NOT NULL DEFAULT (strftime('%s','now')))")
        return conn

    def vectors(self, texts: Sequence[str], encode: Callable[[List[str]], np.ndarray]) -> np.ndarray:
        """One vector per text, encoding only the ones not already stored.

        Any problem with the cache file (locked, corrupt, unwritable) falls back to encoding
        everything: a cache must never be the reason scoring fails.
        """
        keys = [self._key(text) for text in texts]
        found = {}
        try:
            with closing(self._connect()) as conn:
                for start in range(0, len(keys), 500):
                    chunk = keys[start:start + 500]
                    marks = ",".join("?" * len(chunk))
                    for key, dim, blob in conn.execute(
                            f"SELECT key, dim, vec FROM vectors WHERE key IN ({marks})", chunk):
                        vector = np.frombuffer(blob, dtype=np.float32)
                        if vector.size == dim:
                            found[key] = vector
        except (sqlite3.Error, OSError):
            found = {}
        missing = [index for index, key in enumerate(keys) if key not in found]
        self.hits, self.misses = len(keys) - len(missing), len(missing)
        if missing:
            fresh = np.asarray(encode([texts[index] for index in missing]), dtype=np.float32)
            for index, vector in zip(missing, fresh):
                found[keys[index]] = vector
            try:
                with closing(self._connect()) as conn, conn:
                    conn.executemany("INSERT OR REPLACE INTO vectors (key, dim, vec) VALUES (?, ?, ?)",
                                     [(keys[index], int(vector.size), vector.tobytes())
                                      for index, vector in zip(missing, fresh)])
                    conn.execute("DELETE FROM vectors WHERE key IN (SELECT key FROM vectors "
                                 "ORDER BY used_at DESC LIMIT -1 OFFSET ?)", (MAX_ROWS,))
            except (sqlite3.Error, OSError):
                pass
        return np.vstack([found[key] for key in keys]) if keys else np.zeros((0, 0), dtype=np.float32)
