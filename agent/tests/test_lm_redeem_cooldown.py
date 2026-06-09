"""Tests for the LM residual-redeem cooldown (wt-3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent.sandbox.lm_redeem_cooldown import (
    LM_REDEEM_COOLDOWN,
    LMRedeemCooldown,
    read_lm_redeem_cooldown,
    write_lm_redeem_cooldown,
)

_NOW = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)


def test_blocked_within_window_then_clears() -> None:
    cd = LMRedeemCooldown().record({"p1"}, _NOW)
    assert cd.blocked_position_ids(_NOW) == {"p1"}
    assert cd.blocked_position_ids(
        _NOW + LM_REDEEM_COOLDOWN - timedelta(minutes=1)
    ) == {"p1"}
    # Past the window → no longer blocked, so a still-naked position retries.
    assert cd.blocked_position_ids(
        _NOW + LM_REDEEM_COOLDOWN + timedelta(minutes=1)
    ) == set()


def test_record_ignores_empty_ids() -> None:
    cd = LMRedeemCooldown().record({"", "p1"}, _NOW)
    assert set(cd.entries) == {"p1"}


def test_prune_drops_settled_and_expired() -> None:
    cd = LMRedeemCooldown().record({"p1", "p2"}, _NOW)
    # p2 no longer present in lm_positions (settled) → dropped; p1 kept.
    assert set(cd.prune({"p1"}, _NOW).entries) == {"p1"}
    # Past-window entries are dropped even while still live (housekeeping).
    expired = cd.prune({"p1"}, _NOW + LM_REDEEM_COOLDOWN + timedelta(minutes=1))
    assert expired.entries == {}


def test_roundtrip_read_write(tmp_path) -> None:
    path = tmp_path / "lm_redeem_cooldown.json"
    write_lm_redeem_cooldown(LMRedeemCooldown().record({"p1"}, _NOW), path)
    back = read_lm_redeem_cooldown(path)
    assert back.blocked_position_ids(_NOW) == {"p1"}


def test_read_missing_file_returns_empty(tmp_path) -> None:
    assert read_lm_redeem_cooldown(tmp_path / "absent.json").entries == {}


def test_read_corrupt_file_returns_empty(tmp_path) -> None:
    path = tmp_path / "lm_redeem_cooldown.json"
    path.write_text("{ not json")
    assert read_lm_redeem_cooldown(path).entries == {}
