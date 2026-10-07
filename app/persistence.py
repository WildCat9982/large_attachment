"""SQLite: the durable record (documents, chunks, summaries, chat messages).

Redis can be wiped at any time; everything in it is rebuilt from here.
"""
import json
import os
import sqlite3
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    ext TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    kind TEXT,                      -- 'text' or 'table', set by ingestion
    status TEXT NOT NULL,           -- queued | processing | ready | failed
    error TEXT,
    n_chunks INTEGER DEFAULT 0,
    n_chars INTEGER DEFAULT 0,
    meta TEXT,                      -- JSON; table schema for tabular files
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS chunks (
    doc_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    loc TEXT,                       -- e.g. 'p.12' for PDFs
    text TEXT NOT NULL,
    PRIMARY KEY (doc_id, seq)
);
CREATE TABLE IF NOT EXISTS summaries (
    doc_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,           -- queued | running | done | failed
    summary TEXT,
    coverage TEXT,
    error TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT,                      -- JSON; sources or SQL behind an answer
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_doc ON messages (doc_id, id);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(config.SQLITE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init():
    for d in (config.DATA_DIR, config.UPLOAD_DIR, config.TABLE_DIR):
        os.makedirs(d, exist_ok=True)
    with connect() as c:
        c.execute("PRAGMA journal_mode=WAL")  # app and worker share the file
        c.executescript(SCHEMA)


# --- documents ---------------------------------------------------------------

def add_document(doc_id, filename, ext, size_bytes):
    with connect() as c:
        c.execute(
            "INSERT INTO documents (id, filename, ext, size_bytes, status) VALUES (?,?,?,?, 'queued')",
            (doc_id, filename, ext, size_bytes),
        )


def update_document(doc_id, **fields):
    if "meta" in fields and not isinstance(fields["meta"], (str, type(None))):
        fields["meta"] = json.dumps(fields["meta"])
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE documents SET {sets} WHERE id = ?", (*fields.values(), doc_id))


def _doc(row):
    if row is None:
        return None
    d = dict(row)
    d["meta"] = json.loads(d["meta"]) if d["meta"] else {}
    return d


def get_document(doc_id):
    with connect() as c:
        return _doc(c.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone())


def list_documents():
    with connect() as c:
        rows = c.execute("SELECT * FROM documents ORDER BY created_at DESC, rowid DESC").fetchall()
    return [_doc(r) for r in rows]


def delete_document(doc_id):
    with connect() as c:
        for table, col in (("chunks", "doc_id"), ("summaries", "doc_id"),
                           ("messages", "doc_id"), ("documents", "id")):
            c.execute(f"DELETE FROM {table} WHERE {col} = ?", (doc_id,))


# --- chunks ------------------------------------------------------------------

def add_chunks(rows):
    """rows: iterable of (doc_id, seq, loc, text)."""
    with connect() as c:
        c.executemany("INSERT OR REPLACE INTO chunks VALUES (?,?,?,?)", rows)


def delete_chunks(doc_id):
    with connect() as c:
        c.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))


def iter_chunks(doc_id, batch=1000):
    """Yield (seq, loc, text) in document order without loading everything."""
    last = -1
    while True:
        with connect() as c:
            rows = c.execute(
                "SELECT seq, loc, text FROM chunks WHERE doc_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (doc_id, last, batch),
            ).fetchall()
        if not rows:
            return
        for r in rows:
            yield r["seq"], r["loc"], r["text"]
        last = rows[-1]["seq"]


# --- summaries ---------------------------------------------------------------

def set_summary(doc_id, status, summary=None, coverage=None, error=None):
    with connect() as c:
        c.execute(
            "INSERT INTO summaries (doc_id, status, summary, coverage, error) VALUES (?,?,?,?,?) "
            "ON CONFLICT(doc_id) DO UPDATE SET status=excluded.status, summary=excluded.summary, "
            "coverage=excluded.coverage, error=excluded.error, updated_at=CURRENT_TIMESTAMP",
            (doc_id, status, summary, coverage, error),
        )


def get_summary(doc_id):
    with connect() as c:
        row = c.execute("SELECT * FROM summaries WHERE doc_id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


# --- chat messages -----------------------------------------------------------

def add_message(doc_id, role, content, meta=None):
    with connect() as c:
        c.execute(
            "INSERT INTO messages (doc_id, role, content, meta) VALUES (?,?,?,?)",
            (doc_id, role, content, json.dumps(meta) if meta else None),
        )


def get_messages(doc_id, last=None):
    sql = "SELECT role, content, meta FROM messages WHERE doc_id = ? ORDER BY id"
    with connect() as c:
        rows = c.execute(sql, (doc_id,)).fetchall()
    out = [{"role": r["role"], "content": r["content"],
            "meta": json.loads(r["meta"]) if r["meta"] else {}} for r in rows]
    return out[-last:] if last else out
