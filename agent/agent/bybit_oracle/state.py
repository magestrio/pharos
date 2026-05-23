import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_events (
    tx_hash   TEXT    NOT NULL,
    log_index INTEGER NOT NULL,
    event     TEXT    NOT NULL,
    payload   TEXT    NOT NULL,
    seen_at   INTEGER NOT NULL,
    PRIMARY KEY (tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS deposit_requests (
    tx_id                  INTEGER PRIMARY KEY,
    amount                 INTEGER NOT NULL,
    status                 TEXT    NOT NULL,
    bybit_subscription_id  TEXT,
    last_error             TEXT,
    created_at             INTEGER NOT NULL,
    updated_at             INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS withdraw_requests (
    tx_id              INTEGER PRIMARY KEY,
    amount             INTEGER NOT NULL,
    status             TEXT    NOT NULL,
    bybit_withdraw_id  TEXT,
    delivered_amount   INTEGER,
    last_error         TEXT,
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL
);
"""


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def mark_event_processed(
    conn: sqlite3.Connection,
    tx_hash: str,
    log_index: int,
    event: str,
    payload_json: str,
) -> bool:
    """Insert a row into `processed_events`. Returns False if the event was
    already recorded (idempotency check), True if newly inserted.
    """
    try:
        conn.execute(
            "INSERT INTO processed_events (tx_hash, log_index, event, payload, seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (tx_hash, log_index, event, payload_json, int(time.time())),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def upsert_deposit_request(
    conn: sqlite3.Connection,
    tx_id: int,
    amount: int,
    status: str,
) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO deposit_requests (tx_id, amount, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(tx_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
        (tx_id, amount, status, now, now),
    )
    conn.commit()


def upsert_withdraw_request(
    conn: sqlite3.Connection,
    tx_id: int,
    amount: int,
    status: str,
) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO withdraw_requests (tx_id, amount, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(tx_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
        (tx_id, amount, status, now, now),
    )
    conn.commit()
