import sqlite3
from pathlib import Path

import pytest

from agent.bybit_oracle.state import (
    DEPOSIT_CONFIRMED,
    DEPOSIT_ESCROW_WITHDRAWN,
    DEPOSIT_FAILED,
    DEPOSIT_HEDGE_SKIPPED,
    DEPOSIT_HEDGED,
    DEPOSIT_ON_BYBIT,
    DEPOSIT_PRODUCT_SELECTED,
    DEPOSIT_RECEIVED,
    DEPOSIT_STAKED,
    DEPOSIT_SWAPPED,
    advance_deposit_status,
    increment_deposit_retry,
    mark_event_processed,
    open_db,
    upsert_deposit_request,
    upsert_withdraw_request,
)


@pytest.fixture
def db(tmp_path: Path):
    conn = open_db(tmp_path / "test.sqlite")
    yield conn
    conn.close()


def test_schema_creates_tables(db):
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert {"processed_events", "deposit_requests", "withdraw_requests"} <= names


def test_mark_event_processed_idempotent(db):
    inserted = mark_event_processed(db, "0xabc", 0, "DepositRequested", "{}")
    assert inserted is True

    again = mark_event_processed(db, "0xabc", 0, "DepositRequested", "{}")
    assert again is False, "second insert with same (tx_hash, log_index) must be deduped"


def test_mark_event_processed_distinct_log_indexes(db):
    assert mark_event_processed(db, "0xabc", 0, "DepositRequested", "{}") is True
    assert mark_event_processed(db, "0xabc", 1, "WithdrawRequested", "{}") is True


def test_upsert_deposit_creates_then_updates(db):
    upsert_deposit_request(db, tx_id=42, amount=1_000_000, status="received")
    row = db.execute("SELECT * FROM deposit_requests WHERE tx_id = 42").fetchone()
    assert row["amount"] == 1_000_000
    assert row["status"] == "received"
    created = row["created_at"]
    first_updated = row["updated_at"]

    upsert_deposit_request(db, tx_id=42, amount=1_000_000, status="confirmed")
    row = db.execute("SELECT * FROM deposit_requests WHERE tx_id = 42").fetchone()
    assert row["status"] == "confirmed"
    assert row["created_at"] == created, "created_at preserved on upsert"
    assert row["updated_at"] >= first_updated


def test_upsert_withdraw_creates_then_updates(db):
    upsert_withdraw_request(db, tx_id=7, amount=500_000, status="received")
    row = db.execute("SELECT * FROM withdraw_requests WHERE tx_id = 7").fetchone()
    assert row["amount"] == 500_000
    assert row["status"] == "received"

    upsert_withdraw_request(db, tx_id=7, amount=500_000, status="bridging")
    row = db.execute("SELECT * FROM withdraw_requests WHERE tx_id = 7").fetchone()
    assert row["status"] == "bridging"


# --- Deposit FSM (.12a) -----------------------------------------------------


@pytest.fixture
def deposit_row(db):
    upsert_deposit_request(db, tx_id=1, amount=100_000_000, status=DEPOSIT_RECEIVED)
    return db


def test_advance_deposit_happy_path(deposit_row):
    assert advance_deposit_status(
        deposit_row, 1, DEPOSIT_RECEIVED, DEPOSIT_ESCROW_WITHDRAWN
    )
    row = deposit_row.execute("SELECT status FROM deposit_requests WHERE tx_id = 1").fetchone()
    assert row["status"] == DEPOSIT_ESCROW_WITHDRAWN


def test_advance_with_field_writes_column(deposit_row):
    assert advance_deposit_status(
        deposit_row,
        1,
        DEPOSIT_RECEIVED,
        DEPOSIT_ESCROW_WITHDRAWN,
        bybit_deposit_address="0xdeadbeef",
    )
    row = deposit_row.execute(
        "SELECT status, bybit_deposit_address FROM deposit_requests WHERE tx_id = 1"
    ).fetchone()
    assert row["status"] == DEPOSIT_ESCROW_WITHDRAWN
    assert row["bybit_deposit_address"] == "0xdeadbeef"


def test_advance_is_idempotent_returns_false_when_already_advanced(deposit_row):
    # First call: legitimate transition.
    assert advance_deposit_status(deposit_row, 1, DEPOSIT_RECEIVED, DEPOSIT_ESCROW_WITHDRAWN)
    # Crash-restart replay: same call with same expected_from. Returns False
    # because the row is no longer at DEPOSIT_RECEIVED — caller treats this
    # as "step already done, skip forward".
    assert (
        advance_deposit_status(deposit_row, 1, DEPOSIT_RECEIVED, DEPOSIT_ESCROW_WITHDRAWN)
        is False
    )


def test_advance_returns_false_for_nonexistent_tx_id(db):
    assert advance_deposit_status(db, 999, DEPOSIT_RECEIVED, DEPOSIT_ESCROW_WITHDRAWN) is False


def test_advance_illegal_transition_raises(deposit_row):
    # received → on_bybit isn't allowed (must go through escrow_withdrawn first).
    with pytest.raises(ValueError, match="illegal transition"):
        advance_deposit_status(deposit_row, 1, DEPOSIT_RECEIVED, DEPOSIT_ON_BYBIT)


def test_advance_unknown_source_raises(deposit_row):
    # `confirmed` is terminal — no outgoing edges, so it's not in the map keys.
    with pytest.raises(ValueError, match="unknown source status"):
        advance_deposit_status(deposit_row, 1, DEPOSIT_CONFIRMED, DEPOSIT_FAILED)


def test_advance_unknown_field_raises(deposit_row):
    with pytest.raises(ValueError, match="unknown fields"):
        advance_deposit_status(
            deposit_row, 1, DEPOSIT_RECEIVED, DEPOSIT_ESCROW_WITHDRAWN, bogus="x"
        )


def test_failed_is_reachable_from_every_nonterminal_state(deposit_row):
    """The failed state is the manual-intervention escape hatch — every
    in-flight state must be able to reach it without dancing through the FSM.
    """
    chain = [
        (DEPOSIT_RECEIVED, DEPOSIT_ESCROW_WITHDRAWN),
        (DEPOSIT_ESCROW_WITHDRAWN, DEPOSIT_ON_BYBIT),
        (DEPOSIT_ON_BYBIT, DEPOSIT_PRODUCT_SELECTED),
        (DEPOSIT_PRODUCT_SELECTED, DEPOSIT_STAKED),
        (DEPOSIT_STAKED, DEPOSIT_HEDGE_SKIPPED),
        (DEPOSIT_HEDGE_SKIPPED, DEPOSIT_CONFIRMED),
    ]
    # Walk one step into each state then verify advance(state -> FAILED).
    # Reset the row at the start of each iteration so we test fail-from-each
    # in isolation; reuse the same row to avoid creating N tables.
    for src, _next in chain[:-1]:  # confirmed is terminal, can't fail out
        deposit_row.execute(
            "UPDATE deposit_requests SET status = ? WHERE tx_id = 1", (src,)
        )
        assert advance_deposit_status(deposit_row, 1, src, DEPOSIT_FAILED), (
            f"failed should be reachable from {src}"
        )


def test_full_happy_path_usdc(deposit_row):
    """USDC-target deposit: skips swap, picks hedge_skipped (stable asset)."""
    steps = [
        (DEPOSIT_RECEIVED, DEPOSIT_ESCROW_WITHDRAWN, {}),
        (DEPOSIT_ESCROW_WITHDRAWN, DEPOSIT_ON_BYBIT, {"bybit_deposit_address": "0xabc"}),
        (DEPOSIT_ON_BYBIT, DEPOSIT_PRODUCT_SELECTED, {}),
        (DEPOSIT_PRODUCT_SELECTED, DEPOSIT_STAKED, {"bybit_earn_order_id": "ord-1"}),
        (DEPOSIT_STAKED, DEPOSIT_HEDGE_SKIPPED, {}),
        (DEPOSIT_HEDGE_SKIPPED, DEPOSIT_CONFIRMED, {}),
    ]
    for src, dst, fields in steps:
        assert advance_deposit_status(deposit_row, 1, src, dst, **fields), (
            f"happy path stalled at {src} -> {dst}"
        )
    row = deposit_row.execute("SELECT * FROM deposit_requests WHERE tx_id = 1").fetchone()
    assert row["status"] == DEPOSIT_CONFIRMED
    assert row["bybit_deposit_address"] == "0xabc"
    assert row["bybit_earn_order_id"] == "ord-1"


def test_full_happy_path_volatile(deposit_row):
    """Volatile-target deposit: goes through swap, then hedge."""
    steps = [
        (DEPOSIT_RECEIVED, DEPOSIT_ESCROW_WITHDRAWN, {}),
        (DEPOSIT_ESCROW_WITHDRAWN, DEPOSIT_ON_BYBIT, {}),
        (DEPOSIT_ON_BYBIT, DEPOSIT_PRODUCT_SELECTED, {}),
        (DEPOSIT_PRODUCT_SELECTED, DEPOSIT_SWAPPED, {"bybit_swap_order_id": "swap-1"}),
        (DEPOSIT_SWAPPED, DEPOSIT_STAKED, {"bybit_earn_order_id": "stake-1"}),
        (DEPOSIT_STAKED, DEPOSIT_HEDGED, {}),
        (DEPOSIT_HEDGED, DEPOSIT_CONFIRMED, {}),
    ]
    for src, dst, fields in steps:
        assert advance_deposit_status(deposit_row, 1, src, dst, **fields)


def test_increment_retry(deposit_row):
    assert increment_deposit_retry(deposit_row, 1, "bybit timeout") == 1
    assert increment_deposit_retry(deposit_row, 1, "bybit timeout") == 2
    row = deposit_row.execute(
        "SELECT retry_count, last_error, status FROM deposit_requests WHERE tx_id = 1"
    ).fetchone()
    assert row["retry_count"] == 2
    assert row["last_error"] == "bybit timeout"
    assert row["status"] == DEPOSIT_RECEIVED  # retry must NOT change status


def test_increment_retry_unknown_tx_id_raises(db):
    with pytest.raises(LookupError):
        increment_deposit_retry(db, 12345, "x")


def test_open_db_migrates_existing_db_without_new_columns(tmp_path: Path):
    """Simulates an old `.10`-era DB that pre-dates the FSM columns:
    create the legacy schema by hand, then re-open via `open_db` and verify
    the new columns get added without data loss.
    """
    path = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE deposit_requests (
            tx_id INTEGER PRIMARY KEY,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL,
            bybit_subscription_id TEXT,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
    )
    legacy.execute(
        "INSERT INTO deposit_requests (tx_id, amount, status, created_at, updated_at) "
        "VALUES (1, 100, 'received', 0, 0)"
    )
    legacy.commit()
    legacy.close()

    conn = open_db(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(deposit_requests)").fetchall()}
    expected = {
        "bybit_deposit_address",
        "bybit_swap_order_id",
        "bybit_earn_order_id",
        "retry_count",
    }
    assert expected <= cols

    row = conn.execute("SELECT * FROM deposit_requests WHERE tx_id = 1").fetchone()
    assert row["amount"] == 100
    assert row["retry_count"] == 0  # default value applied to legacy row
    conn.close()
