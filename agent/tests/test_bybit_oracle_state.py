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
    WITHDRAW_CONFIRMED,
    WITHDRAW_FAILED,
    WITHDRAW_HEDGE_CLOSE_SKIPPED,
    WITHDRAW_HEDGE_CLOSED,
    WITHDRAW_ON_MANTLE,
    WITHDRAW_RECEIVED,
    WITHDRAW_REDEEMED,
    WITHDRAW_SWAP_SKIPPED,
    WITHDRAW_SWAPPED_TO_USDC,
    advance_deposit_status,
    advance_withdraw_status,
    increment_deposit_retry,
    increment_withdraw_retry,
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


# --- Withdraw FSM (.13a) ----------------------------------------------------


@pytest.fixture
def withdraw_row(db):
    upsert_withdraw_request(db, tx_id=1, amount=50_000_000, status=WITHDRAW_RECEIVED)
    return db


def test_advance_withdraw_happy_path(withdraw_row):
    assert advance_withdraw_status(
        withdraw_row, 1, WITHDRAW_RECEIVED, WITHDRAW_HEDGE_CLOSE_SKIPPED
    )
    row = withdraw_row.execute(
        "SELECT status FROM withdraw_requests WHERE tx_id = 1"
    ).fetchone()
    assert row["status"] == WITHDRAW_HEDGE_CLOSE_SKIPPED


def test_advance_withdraw_with_field_writes_column(withdraw_row):
    assert advance_withdraw_status(
        withdraw_row,
        1,
        WITHDRAW_RECEIVED,
        WITHDRAW_HEDGE_CLOSED,
        bybit_swap_order_id="hedge-close-1",
    )
    row = withdraw_row.execute(
        "SELECT status, bybit_swap_order_id FROM withdraw_requests WHERE tx_id = 1"
    ).fetchone()
    assert row["status"] == WITHDRAW_HEDGE_CLOSED
    assert row["bybit_swap_order_id"] == "hedge-close-1"


def test_advance_withdraw_is_idempotent(withdraw_row):
    assert advance_withdraw_status(
        withdraw_row, 1, WITHDRAW_RECEIVED, WITHDRAW_HEDGE_CLOSE_SKIPPED
    )
    # Replay returns False — row no longer at RECEIVED.
    assert (
        advance_withdraw_status(
            withdraw_row, 1, WITHDRAW_RECEIVED, WITHDRAW_HEDGE_CLOSE_SKIPPED
        )
        is False
    )


def test_advance_withdraw_returns_false_for_unknown_tx(db):
    assert (
        advance_withdraw_status(
            db, 999, WITHDRAW_RECEIVED, WITHDRAW_HEDGE_CLOSE_SKIPPED
        )
        is False
    )


def test_advance_withdraw_illegal_transition_raises(withdraw_row):
    # received → redeemed isn't allowed (must go through hedge step first).
    with pytest.raises(ValueError, match="illegal transition"):
        advance_withdraw_status(withdraw_row, 1, WITHDRAW_RECEIVED, WITHDRAW_REDEEMED)


def test_advance_withdraw_unknown_source_raises(withdraw_row):
    """CONFIRMED is terminal — no outgoing edges, so isn't a valid source."""
    with pytest.raises(ValueError, match="unknown source status"):
        advance_withdraw_status(withdraw_row, 1, WITHDRAW_CONFIRMED, WITHDRAW_FAILED)


def test_advance_withdraw_unknown_field_raises(withdraw_row):
    with pytest.raises(ValueError, match="unknown fields"):
        advance_withdraw_status(
            withdraw_row, 1, WITHDRAW_RECEIVED, WITHDRAW_HEDGE_CLOSE_SKIPPED, bogus="x"
        )


def test_withdraw_failed_reachable_from_every_nonterminal(withdraw_row):
    chain = [
        WITHDRAW_RECEIVED,
        WITHDRAW_HEDGE_CLOSE_SKIPPED,
        WITHDRAW_REDEEMED,
        WITHDRAW_SWAP_SKIPPED,
        WITHDRAW_ON_MANTLE,
    ]
    for src in chain:
        withdraw_row.execute(
            "UPDATE withdraw_requests SET status = ? WHERE tx_id = 1", (src,)
        )
        assert advance_withdraw_status(withdraw_row, 1, src, WITHDRAW_FAILED), (
            f"failed should be reachable from {src}"
        )


def test_full_withdraw_usdc_path(withdraw_row):
    """USDC-staked deposit being withdrawn — skip hedge close, skip swap-back."""
    steps = [
        (WITHDRAW_RECEIVED, WITHDRAW_HEDGE_CLOSE_SKIPPED, {}),
        (WITHDRAW_HEDGE_CLOSE_SKIPPED, WITHDRAW_REDEEMED, {"bybit_earn_redeem_id": "rd-1"}),
        (WITHDRAW_REDEEMED, WITHDRAW_SWAP_SKIPPED, {}),
        (WITHDRAW_SWAP_SKIPPED, WITHDRAW_ON_MANTLE, {
            "bybit_withdraw_id": "wd-1",
            "mantle_tx_hash": "0xdeadbeef",
        }),
        (WITHDRAW_ON_MANTLE, WITHDRAW_CONFIRMED, {"delivered_amount": 50_000_000}),
    ]
    for src, dst, fields in steps:
        assert advance_withdraw_status(withdraw_row, 1, src, dst, **fields), (
            f"happy path stalled at {src} → {dst}"
        )

    row = withdraw_row.execute("SELECT * FROM withdraw_requests WHERE tx_id = 1").fetchone()
    assert row["status"] == WITHDRAW_CONFIRMED
    assert row["bybit_earn_redeem_id"] == "rd-1"
    assert row["bybit_withdraw_id"] == "wd-1"
    assert row["mantle_tx_hash"] == "0xdeadbeef"
    assert row["delivered_amount"] == 50_000_000


def test_full_withdraw_volatile_path(withdraw_row):
    """ETH-staked deposit being withdrawn — hedge close + swap back to USDC."""
    steps = [
        (WITHDRAW_RECEIVED, WITHDRAW_HEDGE_CLOSED, {}),
        (WITHDRAW_HEDGE_CLOSED, WITHDRAW_REDEEMED, {}),
        (WITHDRAW_REDEEMED, WITHDRAW_SWAPPED_TO_USDC, {"bybit_swap_order_id": "swap-back-1"}),
        (WITHDRAW_SWAPPED_TO_USDC, WITHDRAW_ON_MANTLE, {}),
        (WITHDRAW_ON_MANTLE, WITHDRAW_CONFIRMED, {}),
    ]
    for src, dst, fields in steps:
        assert advance_withdraw_status(withdraw_row, 1, src, dst, **fields)


def test_increment_withdraw_retry(withdraw_row):
    assert increment_withdraw_retry(withdraw_row, 1, "bybit timeout") == 1
    assert increment_withdraw_retry(withdraw_row, 1, "bybit timeout") == 2
    row = withdraw_row.execute(
        "SELECT retry_count, last_error, status FROM withdraw_requests WHERE tx_id = 1"
    ).fetchone()
    assert row["retry_count"] == 2
    assert row["last_error"] == "bybit timeout"
    assert row["status"] == WITHDRAW_RECEIVED  # retry must NOT change status


def test_increment_withdraw_retry_unknown_tx_raises(db):
    with pytest.raises(LookupError):
        increment_withdraw_retry(db, 12345, "x")


def test_open_db_migrates_legacy_withdraw_requests(tmp_path: Path):
    """Existing .10-era DB with old withdraw_requests schema must pick up
    new FSM columns on open.
    """
    path = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE withdraw_requests (
            tx_id INTEGER PRIMARY KEY,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL,
            bybit_withdraw_id TEXT,
            delivered_amount INTEGER,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
    )
    legacy.execute(
        "INSERT INTO withdraw_requests (tx_id, amount, status, created_at, updated_at) "
        "VALUES (1, 100, 'received', 0, 0)"
    )
    legacy.commit()
    legacy.close()

    conn = open_db(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(withdraw_requests)").fetchall()}
    expected = {
        "bybit_earn_redeem_id",
        "bybit_swap_order_id",
        "mantle_tx_hash",
        "retry_count",
    }
    assert expected <= cols
    row = conn.execute("SELECT * FROM withdraw_requests WHERE tx_id = 1").fetchone()
    assert row["amount"] == 100
    assert row["retry_count"] == 0
    conn.close()
