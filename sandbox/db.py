"""SQLite storage for the sandbox company (Kestrel Labs): mail + ERP.

The sandbox is deliberately a *real* application with real state, so the agent's
work can be verified by reading that state back — nothing here is mocked for the agent.
"""

import os
import sqlite3
from pathlib import Path

STATE_DIR = Path(os.getenv("SANDBOX_STATE_DIR", Path(__file__).parent / "state"))
DB_PATH = STATE_DIR / "kestrel.db"
ATTACH_DIR = STATE_DIR / "attachments"

SCHEMA = """
CREATE TABLE vendors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email_domain TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    gstin TEXT,
    bank_account TEXT,
    ifsc TEXT,
    status TEXT NOT NULL            -- Approved | Pending verification
);
CREATE TABLE bills (
    id TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL REFERENCES vendors(id),
    invoice_number TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    amount REAL NOT NULL,
    description TEXT,
    status TEXT NOT NULL,           -- Recorded | Payment scheduled | Paid
    payment_date TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE emails (
    id INTEGER PRIMARY KEY,
    folder TEXT NOT NULL,           -- inbox | sent
    sender TEXT NOT NULL,
    sender_name TEXT,
    recipients TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    received_at TEXT NOT NULL,
    unread INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE attachments (
    id INTEGER PRIMARY KEY,
    email_id INTEGER NOT NULL REFERENCES emails(id),
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL
);
CREATE TABLE audit (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows(sql: str, *params) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(sql: str, *params) -> dict | None:
    found = rows(sql, *params)
    return found[0] if found else None


def execute(sql: str, *params) -> None:
    with connect() as conn:
        conn.execute(sql, params)


def kv_get(key: str, default: str | None = None) -> str | None:
    found = row("SELECT v FROM kv WHERE k = ?", key)
    return found["v"] if found else default


def kv_set(key: str, value: str) -> None:
    execute("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", key, value)
