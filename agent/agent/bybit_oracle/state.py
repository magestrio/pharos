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
    bybit_deposit_address  TEXT,
    bybit_swap_order_id    TEXT,
    bybit_earn_order_id    TEXT,
    retry_count            INTEGER NOT NULL DEFAULT 0,
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


# Deposit state machine. Linear except for two branches:
# product_selected → swapped (volatile target) | staked (USDC target, skip swap)
# staked → hedged (volatile) | hedge_skipped (stable)
DEPOSIT_RECEIVED = "received"
DEPOSIT_ESCROW_WITHDRAWN = "escrow_withdrawn"
DEPOSIT_ON_BYBIT = "on_bybit"
DEPOSIT_PRODUCT_SELECTED = "product_selected"
DEPOSIT_SWAPPED = "swapped"
DEPOSIT_STAKED = "staked"
DEPOSIT_HEDGED = "hedged"
DEPOSIT_HEDGE_SKIPPED = "hedge_skipped"
DEPOSIT_CONFIRMED = "confirmed"
DEPOSIT_FAILED = "failed"

# Any non-terminal state may transition to DEPOSIT_FAILED — that's encoded
# explicitly in each entry so the FSM stays self-documenting (no implicit
# escape hatch that future readers have to remember).
DEPOSIT_TRANSITIONS: dict[str, set[str]] = {
    DEPOSIT_RECEIVED: {DEPOSIT_ESCROW_WITHDRAWN, DEPOSIT_FAILED},
    DEPOSIT_ESCROW_WITHDRAWN: {DEPOSIT_ON_BYBIT, DEPOSIT_FAILED},
    DEPOSIT_ON_BYBIT: {DEPOSIT_PRODUCT_SELECTED, DEPOSIT_FAILED},
    DEPOSIT_PRODUCT_SELECTED: {DEPOSIT_SWAPPED, DEPOSIT_STAKED, DEPOSIT_FAILED},
    DEPOSIT_SWAPPED: {DEPOSIT_STAKED, DEPOSIT_FAILED},
    DEPOSIT_STAKED: {DEPOSIT_HEDGED, DEPOSIT_HEDGE_SKIPPED, DEPOSIT_FAILED},
    DEPOSIT_HEDGED: {DEPOSIT_CONFIRMED, DEPOSIT_FAILED},
    DEPOSIT_HEDGE_SKIPPED: {DEPOSIT_CONFIRMED, DEPOSIT_FAILED},
}

# Whitelist of columns advance_deposit_status() will write alongside the
# status change. Prevents accidental SQL injection via fields kwarg and keeps
# the schema-vs-code contract obvious.
_DEPOSIT_UPDATABLE_FIELDS = frozenset(
    {
        "bybit_subscription_id",
        "bybit_deposit_address",
        "bybit_swap_order_id",
        "bybit_earn_order_id",
        "last_error",
    }
)

_DEPOSIT_ADDED_COLUMNS = {
    "bybit_deposit_address": "TEXT",
    "bybit_swap_order_id": "TEXT",
    "bybit_earn_order_id": "TEXT",
    "retry_count": "INTEGER NOT NULL DEFAULT 0",
}


def _migrate_added_columns(
    conn: sqlite3.Connection, table: str, columns: dict[str, str]
) -> None:
    """sqlite has no ADD COLUMN IF NOT EXISTS — introspect and add missing
    ones. Lets existing dev DBs created before this schema bump pick up new
    columns without a drop-and-recreate.
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    have = {r["name"] for r in rows}
    for col, decl in columns.items():
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate_added_columns(conn, "deposit_requests", _DEPOSIT_ADDED_COLUMNS)
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
        "ON CONFLICT(tx_id) DO UPDATE SET "
        "  status = excluded.status, updated_at = excluded.updated_at",
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
        "ON CONFLICT(tx_id) DO UPDATE SET "
        "  status = excluded.status, updated_at = excluded.updated_at",
        (tx_id, amount, status, now, now),
    )
    conn.commit()


def advance_deposit_status(
    conn: sqlite3.Connection,
    tx_id: int,
    expected_from: str,
    new_status: str,
    **fields: str | None,
) -> bool:
    """Atomic compare-and-swap state transition.

    Returns True iff the row was at `expected_from` and is now at `new_status`.
    Returns False if the row had already advanced past `expected_from` (or
    didn't exist) — callers should treat this as "step already done by a
    prior crash-restart" and skip forward, NOT retry. This is the idempotency
    primitive: every handler step does `if not advance(...): return early`.

    Raises ValueError on illegal transition per DEPOSIT_TRANSITIONS — that's
    a code bug, not runtime data, so failing loud is correct.
    """
    allowed = DEPOSIT_TRANSITIONS.get(expected_from)
    if allowed is None:
        raise ValueError(f"unknown source status {expected_from!r}")
    if new_status not in allowed:
        raise ValueError(
            f"illegal transition {expected_from!r} -> {new_status!r}; "
            f"allowed: {sorted(allowed)}"
        )

    unknown = set(fields) - _DEPOSIT_UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")

    set_clauses = ["status = :new_status", "updated_at = :now"]
    params: dict[str, str | int | None] = {
        "tx_id": tx_id,
        "expected_from": expected_from,
        "new_status": new_status,
        "now": int(time.time()),
    }
    for key, value in fields.items():
        set_clauses.append(f"{key} = :{key}")
        params[key] = value

    cur = conn.execute(
        f"UPDATE deposit_requests SET {', '.join(set_clauses)} "
        f"WHERE tx_id = :tx_id AND status = :expected_from",
        params,
    )
    conn.commit()
    return cur.rowcount == 1


def increment_deposit_retry(
    conn: sqlite3.Connection,
    tx_id: int,
    error: str,
) -> int:
    """Bump retry_count and store last_error without touching status.

    Returns the new retry_count (lets the caller enforce a max-retry policy).
    """
    cur = conn.execute(
        "UPDATE deposit_requests "
        "SET retry_count = retry_count + 1, last_error = ?, updated_at = ? "
        "WHERE tx_id = ? RETURNING retry_count",
        (error, int(time.time()), tx_id),
    )
    row = cur.fetchone()
    conn.commit()
    if row is None:
        raise LookupError(f"deposit_requests row for tx_id={tx_id} not found")
    return int(row[0])
