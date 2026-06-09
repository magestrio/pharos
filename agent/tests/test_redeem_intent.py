"""Tests for the hedged-Earn exit-intent store."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from agent.sandbox.redeem_intent import (
    RedeemExitIntent,
    RedeemIntentState,
    read_redeem_intents,
    write_redeem_intents,
)


def _intent(product_id: str = "TON-FLEX", coin: str = "TON") -> RedeemExitIntent:
    return RedeemExitIntent(
        coin=coin,
        product_id=product_id,
        category="FlexibleSaving",
        opened_at=datetime.now(UTC),
        expected_redeem_native=Decimal("100"),
        baseline_wallet_native=Decimal("2"),
        redeem_order_link_id="lnk-1",
        paired_perp_symbol=f"{coin}USDT",
        perp_qty_base=Decimal("100"),
    )


def test_roundtrip(tmp_path: Path):
    path = tmp_path / "redeem_intent.json"
    state = RedeemIntentState(intents=[_intent()])
    write_redeem_intents(state, path)
    loaded = read_redeem_intents(path)
    assert loaded.active_product_ids() == {"TON-FLEX"}
    got = loaded.get("TON-FLEX")
    assert got is not None
    assert got.expected_redeem_native == Decimal("100")
    assert got.paired_perp_symbol == "TONUSDT"


def test_missing_returns_empty(tmp_path: Path):
    assert read_redeem_intents(tmp_path / "nope.json").intents == []


def test_corrupt_returns_empty(tmp_path: Path):
    path = tmp_path / "redeem_intent.json"
    path.write_text("{broken json")
    assert read_redeem_intents(path).intents == []


def test_invalid_schema_returns_empty(tmp_path: Path):
    path = tmp_path / "redeem_intent.json"
    path.write_text('{"intents": [{"coin": "TON"}]}')  # missing required fields
    assert read_redeem_intents(path).intents == []


def test_upsert_replaces_by_product_id():
    state = RedeemIntentState(intents=[_intent()])
    updated = _intent()
    updated.expected_redeem_native = Decimal("250")
    state2 = state.upsert(updated)
    assert len(state2.intents) == 1
    assert state2.get("TON-FLEX").expected_redeem_native == Decimal("250")


def test_upsert_appends_new_product():
    state = RedeemIntentState(intents=[_intent("TON-FLEX", "TON")])
    state2 = state.upsert(_intent("SOL-ON", "SOL"))
    assert state2.active_product_ids() == {"TON-FLEX", "SOL-ON"}


def test_remove():
    state = RedeemIntentState(intents=[_intent("TON-FLEX"), _intent("SOL-ON", "SOL")])
    state2 = state.remove("TON-FLEX")
    assert state2.active_product_ids() == {"SOL-ON"}
    # removing a missing id is a no-op
    assert state2.remove("ZZZ").active_product_ids() == {"SOL-ON"}
