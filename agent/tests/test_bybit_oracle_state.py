from pathlib import Path

import pytest

from agent.bybit_oracle.state import (
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
