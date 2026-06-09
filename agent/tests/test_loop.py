"""Loop driver tests (`.13`).

`run_one_cycle` is the unit under test — `run_loop` is a thin while-loop
around it. Bybit + Anthropic clients are mocked; snapshot / decision /
execution writes go to `tmp_path` via patched module constants so the
real `agent/sandbox/snapshots/` etc. aren't polluted.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.bybit_oracle.bybit_client import EarnOrderResult
from agent.reason.schema import Decision, Pick, VenueAllocation
from agent.sandbox.decide import DecisionUsage
from agent.sandbox.loop import run_loop, run_one_cycle


def _stub_usage() -> DecisionUsage:
    """Minimal usage stub for mocked `decide()` returns. Tests that
    care about specific token counts override fields per-call."""
    return DecisionUsage(
        model="claude-sonnet-4-6",
        input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=0,
        estimated_cost_usd=Decimal("0"),
    )
from agent.sandbox.snapshot import (
    MarketSnapshot,
    PerpInfo,
    ProductSummary,
    Snapshot,
    UsdcPegSnapshot,
    WalletSnapshot,
)


@pytest.fixture(autouse=True)
def _stub_watcher_baseline_update(tmp_path: Path):
    """`event-driven-rebalance.3` plumbed an `update_baseline_from_snapshot`
    call inside `run_one_cycle` (writes `state/watcher-baseline.json`).
    Stub it out across this file so existing cycle/loop tests don't
    pollute the on-disk state.

    Also redirects the safety-net constants (`HALT_FILE`,
    `EQUITY_HISTORY_FILE`) to `tmp_path` so each test sees a clean
    halt / drawdown world. Without this, a previous test that creates
    a halt marker would block subsequent tests, and equity-history
    rows would leak across tests."""
    halt_path = tmp_path / "HALT"
    equity_path = tmp_path / "equity.jsonl"
    with (
        patch(
            "agent.sandbox.loop.update_baseline_from_snapshot",
            lambda *_a, **_kw: None,
        ),
        patch("agent.sandbox.safety.HALT_FILE", halt_path),
        patch("agent.sandbox.safety.EQUITY_HISTORY_FILE", equity_path),
    ):
        yield


def _snapshot(total_equity_usd: str = "100") -> Snapshot:
    return Snapshot(
        captured_at=datetime.now(UTC),
        wallet=WalletSnapshot(total_equity_usd=Decimal(total_equity_usd)),
        earn_positions=[],
        lm_positions=[],
        products={
            "FlexibleSaving": [
                ProductSummary(
                    category="FlexibleSaving",
                    product_id="1131",
                    coin="USD1",
                    effective_apr=Decimal("0.0752"),
                    apr_source="estimate_apr",
                    base_apr_string=None,
                    redeem_lockup_minutes=None,
                    notes=[],
                )
            ],
            "OnChain": [],
            "LiquidityMining": [],
        },
        market=MarketSnapshot(),
        perp_market={},
        usdc_peg=UsdcPegSnapshot(
            price_usd=Decimal("1.0"),
            deviation_bps=Decimal("0"),
            fetched_at=datetime.now(UTC),
        ),
        errors=[],
    )


def _decision_clean() -> Decision:
    return Decision(
        thesis="placeholder happy-path decision for cycle tests.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=0.6),
            VenueAllocation(
                venue_id="bybit_flex",
                weight=0.4,
                picks=[Pick(product_id="1131", weight=1.0)],
            ),
        ],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=4.0,
    )


def _snapshot_with_ton(
    *, liquid_usdc: str, liquid_usdt: str = "0", total_equity_usd: str = "100",
    ton_held: str = "0", ton_funding_7d: str | None = None,
    ton_interval: str = "8", ton_apr: str = "0.18",
) -> Snapshot:
    """Small-vault snapshot with a TON OnChain product + perp, for the
    liquid-budget clamp tests. `ton_held` (native) seeds an OnChain TON
    position so net-new vs held can be exercised. `ton_funding_7d` sets the
    perp's signed per-period 7d-avg funding so the sub-floor clamp (.66) can
    be exercised; None ⇒ no funding signal (not sub-floor)."""
    earn = (
        [{"productId": "8", "coin": "TON", "amount": ton_held,
          "category": "OnChain", "status": "Active"}]
        if Decimal(ton_held) > 0 else []
    )
    ton_perp = PerpInfo(
        symbol="TONUSDT", mark_price=Decimal("2.0"),
        min_notional_usd=Decimal("1.0"),
        funding_rate_7d_avg=Decimal(ton_funding_7d) if ton_funding_7d else None,
        funding_interval_hours=Decimal(ton_interval),
    )
    return Snapshot(
        captured_at=datetime.now(UTC),
        wallet=WalletSnapshot(
            total_equity_usd=Decimal(total_equity_usd),
            liquid_usdc_usd=Decimal(liquid_usdc),
            liquid_usdt_usd=Decimal(liquid_usdt),
        ),
        earn_positions=earn,
        lm_positions=[],
        products={
            "FlexibleSaving": [],
            "OnChain": [ProductSummary(
                category="OnChain", product_id="8", coin="TON",
                effective_apr=Decimal(ton_apr), apr_source="estimate_apr",
                base_apr_string=None, redeem_lockup_minutes=None, notes=[],
            )],
            "LiquidityMining": [],
        },
        market=MarketSnapshot(),
        perp_market={"TON": ton_perp},
        usdc_peg=UsdcPegSnapshot(price_usd=Decimal("1.0"), deviation_bps=Decimal("0"),
                                 fetched_at=datetime.now(UTC)),
        errors=[],
    )


def test_clamp_drops_overbudget_new_nonstable_to_cash() -> None:
    """`.67`: a NEW non-stable pick ($20) exceeding `max_new_nonstable`
    ($6/2.05≈$2.93 on a $6-liquid book) is rolled into cash deterministically
    (the LLM ignores the advisory)."""
    from agent.sandbox.loop import _clamp_to_liquid_budget
    snap = _snapshot_with_ton(liquid_usdc="6")
    dec = Decision(
        thesis="over-commit a fresh TON OnChain pick past the liquid budget.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=0.8),
            VenueAllocation(venue_id="bybit_onchain", weight=0.2,
                            picks=[Pick(product_id="8", weight=1.0)]),  # $20 new
        ],
        hedges=[], confidence=0.7, risk_flags=[], notes=[],
        expected_blended_apr_pct=5.0,
    )
    new_dict, dropped, note = _clamp_to_liquid_budget(dec.model_dump(), snap)
    assert dropped == ["8"]
    assert "liquid_clamp" in (note or "")
    # The decision must still be valid shape (weights sum to 1.0).
    rebuilt = Decision.model_validate(new_dict)
    assert abs(sum(v.weight for v in rebuilt.venues) - 1.0) < 1e-6
    assert not any(
        p.product_id == "8"
        for v in rebuilt.venues for p in v.picks
    )


def test_clamp_drops_overbudget_carry_to_cash() -> None:
    """2026-06-08: a NEW funding-carry pick (spot Buy + perp short, ~2.05×
    USDT draw) over the liquid budget is clamped to cash like a hedged
    non-stable — else it survives to `check_stable_spend_cap` and strands
    the cycle skipped:invalid (prod: HYPE carry $18.23 vs $10.41 liquid)."""
    from agent.sandbox.loop import _clamp_to_liquid_budget
    snap = _snapshot_with_ton(liquid_usdc="6")  # max_new_nonstable ≈ $2.93
    snap.products["FundingCarry"] = [
        ProductSummary(
            category="FundingCarry", product_id="HYPEUSDT", coin="HYPE",
            effective_apr=Decimal("0.08"), apr_source="funding_carry",
            base_apr_string=None, redeem_lockup_minutes=0, notes=[],
        ),
    ]
    dec = Decision(
        thesis="open a HYPE funding-carry past the liquid budget.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=0.85),
            VenueAllocation(venue_id="bybit_funding_carry", weight=0.15,
                            picks=[Pick(product_id="HYPEUSDT", weight=1.0)]),  # $15 new
        ],
        hedges=[], confidence=0.7, risk_flags=[], notes=[],
        expected_blended_apr_pct=6.0,
    )
    new_dict, dropped, note = _clamp_to_liquid_budget(dec.model_dump(), snap)
    assert dropped == ["HYPEUSDT"]
    assert "liquid_clamp" in (note or "")
    rebuilt = Decision.model_validate(new_dict)
    assert abs(sum(v.weight for v in rebuilt.venues) - 1.0) < 1e-6
    assert not any(
        p.product_id == "HYPEUSDT" for v in rebuilt.venues for p in v.picks
    )


def test_strip_carry_coins_drops_targeted_carry_to_cash() -> None:
    """`_strip_carry_coins_from_decision` removes the targeted carry pick and
    rolls its weight to cash — so a `carry_liq_close` makes the diff CLOSE (not
    re-OPEN) the carry on an executing cycle (loop-1/wt-2). No-op when the coin
    isn't a current carry target."""
    from agent.sandbox.loop import _strip_carry_coins_from_decision
    snap = _snapshot_with_ton(liquid_usdc="100")
    snap.products["FundingCarry"] = [
        ProductSummary(
            category="FundingCarry", product_id="HYPEUSDT", coin="HYPE",
            effective_apr=Decimal("0.08"), apr_source="funding_carry",
            base_apr_string=None, redeem_lockup_minutes=0, notes=[],
        ),
    ]
    dec = Decision(
        thesis="hold a HYPE funding-carry while funding stays positive.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=0.85),
            VenueAllocation(venue_id="bybit_funding_carry", weight=0.15,
                            picks=[Pick(product_id="HYPEUSDT", weight=1.0)]),
        ],
        hedges=[], confidence=0.7, risk_flags=[], notes=[],
        expected_blended_apr_pct=6.0,
    )
    stripped, dropped = _strip_carry_coins_from_decision(dec, snap, {"HYPE"})
    assert dropped == ["HYPEUSDT"]
    assert not any(
        p.product_id == "HYPEUSDT" for v in stripped.venues for p in v.picks
    )
    assert abs(sum(v.weight for v in stripped.venues) - 1.0) < 1e-6

    same, none_dropped = _strip_carry_coins_from_decision(dec, snap, {"SOL"})
    assert none_dropped == []
    assert same.venue("bybit_funding_carry") is not None


def test_clamp_keeps_held_position_at_size() -> None:
    """A HELD TON position kept at its current size (net_new≈0) is NOT
    dropped, even though its gross value exceeds the liquid budget."""
    from agent.sandbox.loop import _clamp_to_liquid_budget
    # 10 TON × $2 = $20 held; liquid only $6.
    snap = _snapshot_with_ton(liquid_usdc="6", ton_held="10")
    dec = Decision(
        thesis="keep the existing TON OnChain position at its current size.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=0.8),
            VenueAllocation(venue_id="bybit_onchain", weight=0.2,
                            picks=[Pick(product_id="8", weight=1.0)]),  # $20 target == held
        ],
        hedges=[], confidence=0.7, risk_flags=[], notes=[],
        expected_blended_apr_pct=5.0,
    )
    new_dict, dropped, note = _clamp_to_liquid_budget(dec.model_dump(), snap)
    assert dropped == []
    assert note is None


def test_clamp_noop_when_within_budget() -> None:
    """Ample liquid → nothing dropped."""
    from agent.sandbox.loop import _clamp_to_liquid_budget
    snap = _snapshot_with_ton(liquid_usdc="100")  # max_new_nonstable ≈ $48.8
    dec = Decision(
        thesis="a small fresh TON pick well within the liquid budget.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=0.9),
            VenueAllocation(venue_id="bybit_onchain", weight=0.1,
                            picks=[Pick(product_id="8", weight=1.0)]),  # $10 new
        ],
        hedges=[], confidence=0.7, risk_flags=[], notes=[],
        expected_blended_apr_pct=5.0,
    )
    new_dict, dropped, note = _clamp_to_liquid_budget(dec.model_dump(), snap)
    assert dropped == []


def _overbudget_ton_decision() -> Decision:
    return Decision(
        thesis="over-commit a fresh TON OnChain pick past the liquid budget.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=0.8),
            VenueAllocation(venue_id="bybit_onchain", weight=0.2,
                            picks=[Pick(product_id="8", weight=1.0)]),  # $20 new
        ],
        hedges=[], confidence=0.7, risk_flags=[], notes=[],
        expected_blended_apr_pct=5.0,
    )


def test_clamp_skips_when_snapshot_stale() -> None:
    """`.69` freshness guard: when the snapshot fed to decide() and the one
    clamped against diverge (a future snapshot-reuse refactor), skip the
    clamp rather than use a stale budget — even on an over-budget pick that
    would otherwise be dropped."""
    from agent.sandbox.loop import _clamp_to_liquid_budget
    snap = _snapshot_with_ton(liquid_usdc="6")
    new_dict, dropped, note = _clamp_to_liquid_budget(
        _overbudget_ton_decision().model_dump(), snap,
        decide_captured_at="2020-01-01T00:00:00+00:00",  # != snap.captured_at
    )
    assert dropped == []
    assert note is None


def test_clamp_runs_when_snapshot_fresh() -> None:
    """`.69`: a matching `decide_captured_at` (the snapshot decide saw)
    leaves the clamp active — the over-budget pick is still dropped. Guards
    against the freshness check mis-firing on a normal cycle."""
    from agent.sandbox.loop import _clamp_to_liquid_budget
    snap = _snapshot_with_ton(liquid_usdc="6")
    new_dict, dropped, note = _clamp_to_liquid_budget(
        _overbudget_ton_decision().model_dump(), snap,
        decide_captured_at=snap.captured_at.isoformat(),
    )
    assert dropped == ["8"]


# ─── sub-floor non-stable growth clamp (.66) ─────────────────────────────────


def test_subfloor_clamp_clamps_grown_subfloor_to_held() -> None:
    """(.66) The LLM grows a held sub-floor-funding non-stable ($10 held → $20
    target). check_funding_rate_floor would reject the growth; the clamp trims
    the pick to its current held size and parks the freed weight in cash so
    the cycle validates."""
    from agent.sandbox.loop import _clamp_subfloor_nonstable_growth
    # 5 TON × $2 = $10 held; funding −0.0003/8h ≈ −32.85%/yr (sub-floor).
    snap = _snapshot_with_ton(liquid_usdc="50", ton_held="5", ton_funding_7d="-0.0003")
    dec = Decision(
        thesis="grow the held sub-floor TON OnChain position past current size.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=0.8),
            VenueAllocation(venue_id="bybit_onchain", weight=0.2,
                            picks=[Pick(product_id="8", weight=1.0)]),  # $20 target
        ],
        hedges=[], confidence=0.7, risk_flags=[], notes=[],
        expected_blended_apr_pct=5.0,
    )
    new_dict, clamped, note = _clamp_subfloor_nonstable_growth(dec.model_dump(), snap)
    assert clamped == ["8"]
    assert "subfloor_clamp" in (note or "")
    rebuilt = Decision.model_validate(new_dict)
    assert abs(sum(v.weight for v in rebuilt.venues) - 1.0) < 1e-6
    onchain = next(v for v in rebuilt.venues if v.venue_id == "bybit_onchain")
    # Clamped to held: $10 / $100 book = 0.10 effective.
    assert abs(onchain.weight * onchain.picks[0].weight - 0.10) < 1e-6
    cash = next(v for v in rebuilt.venues if v.venue_id == "cash_usdc")
    assert abs(cash.weight - 0.90) < 1e-6  # 0.8 + 0.10 freed


def test_subfloor_clamp_keeps_net_positive_highapr_pick() -> None:
    """Regression for the live 2026-06-08 ME bug: a NEW non-stable pick with
    deeply negative funding (−32.85%/yr, raw sub-floor) but a high Earn APR
    that more than covers it (net-of-hedge POSITIVE) must NOT be clamped — the
    old clamp gated raw funding and dumped a +35%-net pick to cash, leaving the
    vault in ~3% stables. The net-aware clamp must keep it (the validator
    passes it)."""
    from agent.sandbox.loop import _clamp_subfloor_nonstable_growth
    # gross 0.70 + annual(−0.0003/8h ≈ −0.3285) − friction 0.018 = +0.3535 net.
    snap = _snapshot_with_ton(
        liquid_usdc="50", ton_held="0", ton_funding_7d="-0.0003", ton_apr="0.70"
    )
    dec = Decision(
        thesis="open a high-APR hedged TON OnChain pick; net yield is positive.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=0.8),
            VenueAllocation(venue_id="bybit_onchain", weight=0.2,
                            picks=[Pick(product_id="8", weight=1.0)]),  # $20 new
        ],
        hedges=[], confidence=0.7, risk_flags=[], notes=[],
        expected_blended_apr_pct=10.0,
    )
    new_dict, clamped, note = _clamp_subfloor_nonstable_growth(dec.model_dump(), snap)
    assert clamped == [], note
    assert new_dict == dec.model_dump()  # untouched


def test_subfloor_clamp_exempts_held_at_current() -> None:
    """A held sub-floor position KEPT at current size (net_new≈0) is exempt —
    matching check_funding_rate_floor's net-new-only gate."""
    from agent.sandbox.loop import _clamp_subfloor_nonstable_growth
    snap = _snapshot_with_ton(liquid_usdc="50", ton_held="10", ton_funding_7d="-0.0003")
    dec = Decision(
        thesis="keep the held sub-floor TON position at its current size.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=0.8),
            VenueAllocation(venue_id="bybit_onchain", weight=0.2,
                            picks=[Pick(product_id="8", weight=1.0)]),  # $20 == held
        ],
        hedges=[], confidence=0.7, risk_flags=[], notes=[],
        expected_blended_apr_pct=5.0,
    )
    _, clamped, note = _clamp_subfloor_nonstable_growth(dec.model_dump(), snap)
    assert clamped == []
    assert note is None


def test_subfloor_clamp_noop_when_funding_above_floor() -> None:
    """Funding above the floor → growing is legal → no clamp."""
    from agent.sandbox.loop import _clamp_subfloor_nonstable_growth
    snap = _snapshot_with_ton(liquid_usdc="50", ton_held="0", ton_funding_7d="0.00005")
    dec = Decision(
        thesis="open a fresh TON pick whose funding is comfortably above floor.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=0.8),
            VenueAllocation(venue_id="bybit_onchain", weight=0.2,
                            picks=[Pick(product_id="8", weight=1.0)]),
        ],
        hedges=[], confidence=0.7, risk_flags=[], notes=[],
        expected_blended_apr_pct=5.0,
    )
    _, clamped, _ = _clamp_subfloor_nonstable_growth(dec.model_dump(), snap)
    assert clamped == []


def test_subfloor_clamp_preserves_other_picks_in_venue() -> None:
    """In a multi-pick venue, only the sub-floor non-stable pick is clamped;
    the sibling stable pick keeps its ABSOLUTE effective weight (renormalized
    within the shrunk venue)."""
    from agent.sandbox.loop import _clamp_subfloor_nonstable_growth
    snap = Snapshot(
        captured_at=datetime.now(UTC),
        wallet=WalletSnapshot(
            total_equity_usd=Decimal("100"),
            liquid_usdc_usd=Decimal("50"), liquid_usdt_usd=Decimal("0"),
        ),
        earn_positions=[], lm_positions=[],
        products={
            "FlexibleSaving": [],
            "OnChain": [
                ProductSummary(category="OnChain", product_id="26", coin="USDC",
                               effective_apr=Decimal("0.034"), apr_source="apr_history"),
                ProductSummary(category="OnChain", product_id="8", coin="TON",
                               effective_apr=Decimal("0.18"), apr_source="estimate_apr"),
            ],
            "LiquidityMining": [],
        },
        market=MarketSnapshot(),
        perp_market={"TON": PerpInfo(symbol="TONUSDT", mark_price=Decimal("2.0"),
                                     min_notional_usd=Decimal("1.0"),
                                     funding_rate_7d_avg=Decimal("-0.0003"),
                                     funding_interval_hours=Decimal("8"))},
        usdc_peg=UsdcPegSnapshot(price_usd=Decimal("1.0"), deviation_bps=Decimal("0"),
                                 fetched_at=datetime.now(UTC)),
        errors=[],
    )
    dec = Decision(
        thesis="USDC OnChain plus a fresh sub-floor TON pick in the same venue.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=0.5),
            VenueAllocation(venue_id="bybit_onchain", weight=0.5,
                            picks=[Pick(product_id="26", weight=0.79),
                                   Pick(product_id="8", weight=0.21)]),
        ],
        hedges=[], confidence=0.7, risk_flags=[], notes=[],
        expected_blended_apr_pct=5.0,
    )
    new_dict, clamped, _ = _clamp_subfloor_nonstable_growth(dec.model_dump(), snap)
    assert clamped == ["8"]
    rebuilt = Decision.model_validate(new_dict)
    assert abs(sum(v.weight for v in rebuilt.venues) - 1.0) < 1e-6
    onchain = next(v for v in rebuilt.venues if v.venue_id == "bybit_onchain")
    # TON (8) dropped (clamped to held=0); USDC (26) keeps absolute eff 0.395.
    assert [p.product_id for p in onchain.picks] == ["26"]
    assert abs(onchain.weight * onchain.picks[0].weight - 0.395) < 1e-6
    cash = next(v for v in rebuilt.venues if v.venue_id == "cash_usdc")
    assert abs(cash.weight - 0.605) < 1e-6  # 0.5 + 0.105 freed


def _decision_with_risk_flag() -> Decision:
    return Decision(
        thesis="risk-off; flagging cycle to abort intentionally.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=1.0),
        ],
        hedges=[],
        confidence=0.7,
        risk_flags=["depeg-suspected"],  # validator rejects on non-empty
        notes=[],
        expected_blended_apr_pct=0.0,
    )


@pytest.mark.asyncio
async def test_run_one_cycle_happy_path_dry_run(tmp_path: Path) -> None:
    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot()
    decision = _decision_clean()

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch(
            "agent.sandbox.loop._load_recent_prior_decisions",
            lambda *_a, **_kw: [],
        ),
        patch(
            "agent.sandbox.loop.decide",
            AsyncMock(return_value=(decision, _stub_usage())),
        ),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp, **_kw: tmp_path / "decision.json",
        ),
    ):
        # write a placeholder snapshot json so `snap_path.read_text()` succeeds
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=False, yes=False, min_confidence=0.6
        )

    assert outcome["result"] == "ok"
    assert outcome["validator_ok"] is True
    assert outcome["confidence"] == 0.7
    # Cash-only decision → no actions planned → returns "no_actions" actually
    # Wait — with cash 0.5 + flex 0.5 USD1, actions ARE planned. But our
    # snapshot has wallet=$100, no earn_positions, so flex_usd=$50
    # subscribe expected.
    assert "execute" in outcome["stages"]
    assert outcome["actions_planned"] >= 1


@pytest.mark.asyncio
async def test_run_one_cycle_validator_failure_short_circuits(tmp_path: Path) -> None:
    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot()
    bad_decision = _decision_with_risk_flag()

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(bad_decision, _stub_usage()))),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp, **_kw: tmp_path / "decision.json",
        ),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=False, yes=False, min_confidence=0.6
        )

    assert outcome["result"] == "skipped:invalid"
    assert outcome["validator_ok"] is False
    assert any("risk_flags" in e for e in outcome["validator_errors"])
    # Stage list stops at validate — no diff / approval / execute.
    assert "validate" in outcome["stages"]
    assert "diff" not in outcome["stages"]


@pytest.mark.asyncio
async def test_run_one_cycle_derisk_sweep_runs_on_subconfidence(tmp_path: Path) -> None:
    """`bybit-sandbox` 2026-06-08: naked non-stable stranded in FUND (TIA
    freed by an LM redeem) must be de-risked even when the allocation cycle
    is below the auto-approve floor — the agent ran 0.52-0.58 cycles for days
    while ~17 TIA sat naked. The safety sweep executes a LIVE orphan Sell
    independent of the allocation's approval."""
    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot()
    snap.wallet.fund_coin_balances = {"TIA": Decimal("17.08")}  # naked in FUND
    snap.perp_market = {
        "TIA": PerpInfo(
            symbol="TIAUSDT", mark_price=Decimal("0.32"),
            min_notional_usd=Decimal("1.0"),
            min_order_qty=Decimal("0.1"), qty_step=Decimal("0.1"),
        )
    }
    # Valid allocation but sub-floor confidence → allocation won't execute.
    decision = _decision_clean().model_copy(update={"confidence": 0.5})

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: []),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(decision, _stub_usage()))),
        patch("agent.sandbox.loop.write_decision", lambda d, sp, **_kw: tmp_path / "decision.json"),
        patch("agent.sandbox.loop.execute_actions", AsyncMock(return_value=[])) as exec_mock,
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=True, yes=True, min_confidence=0.6
        )

    # The de-risk sweep ran LIVE (dry_run=False) with a TIAUSDT Sell, even
    # though the allocation was sub-floor.
    live_sells = [
        c for c in exec_mock.call_args_list
        if c.kwargs.get("dry_run") is False
        and any(
            a.product_id == "TIAUSDT" and a.side == "Sell"
            for a in c.args[1]
        )
    ]
    assert live_sells, exec_mock.call_args_list
    assert outcome.get("safety_sweep") is not None


@pytest.mark.asyncio
async def test_run_one_cycle_derisk_sweep_skipped_on_full_live(tmp_path: Path) -> None:
    """On a valid, conf>=floor cycle the main diff already emits the orphan
    sell, so the standalone sweep must NOT also fire — exactly one
    execute_actions call, no separate `safety_sweep`."""
    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot()
    snap.wallet.fund_coin_balances = {"TIA": Decimal("17.08")}
    snap.perp_market = {
        "TIA": PerpInfo(
            symbol="TIAUSDT", mark_price=Decimal("0.32"),
            min_notional_usd=Decimal("1.0"),
            min_order_qty=Decimal("0.1"), qty_step=Decimal("0.1"),
        )
    }
    decision = _decision_clean()  # confidence 0.7 >= floor

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: []),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(decision, _stub_usage()))),
        patch("agent.sandbox.loop.write_decision", lambda d, sp, **_kw: tmp_path / "decision.json"),
        patch("agent.sandbox.loop.execute_actions", AsyncMock(return_value=[])) as exec_mock,
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=True, yes=True, min_confidence=0.6
        )

    assert exec_mock.call_count == 1  # only the main diff, no separate sweep
    assert outcome.get("safety_sweep") is None


@pytest.mark.asyncio
async def test_run_one_cycle_derisk_sweep_closes_near_liq_carry(tmp_path: Path) -> None:
    """A `carry_liq_close` wake event on a sub-floor cycle closes the carry
    LIVE via the safety sweep — carry has no pick to drop, so this is its only
    deterministic exit when the allocation won't execute."""
    from agent.sandbox.carry_state import CarryPositionRecord, CarryState
    from agent.sandbox.execute import ActionKind

    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot()
    decision = _decision_clean().model_copy(update={"confidence": 0.5})
    carry_state = CarryState(positions=[
        CarryPositionRecord(
            coin="TON",
            opened_at=datetime.now(UTC),
            target_pick_usd=Decimal("100"),
            spot_qty_base=Decimal("50"),
            perp_qty_base=Decimal("50"),
            mark_price_at_open=Decimal("2.0"),
            spot_order_link_id="x_spot",
            perp_order_link_id="x_perp",
        )
    ])
    wake_events = [{"kind": "carry_liq_close", "coin": "TON",
                    "position_id": "perp:TONUSDT"}]

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: []),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(decision, _stub_usage()))),
        patch("agent.sandbox.loop.write_decision", lambda d, sp, **_kw: tmp_path / "decision.json"),
        patch("agent.sandbox.loop.read_carry_state", lambda *_a, **_kw: carry_state),
        patch("agent.sandbox.loop.execute_actions", AsyncMock(return_value=[])) as exec_mock,
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=True, yes=True, min_confidence=0.6,
            wake_events=wake_events,
        )

    live_carry_closes = [
        c for c in exec_mock.call_args_list
        if c.kwargs.get("dry_run") is False
        and any(
            a.kind == ActionKind.CLOSE_FUNDING_CARRY and a.coin == "TON"
            for a in c.args[1]
        )
    ]
    assert live_carry_closes, exec_mock.call_args_list
    assert outcome.get("safety_sweep") is not None


@pytest.mark.asyncio
async def test_run_one_cycle_derisk_sweep_reconciles_carry_state(tmp_path: Path) -> None:
    """After the sweep closes a near-liq carry on Bybit, the closure is rolled
    back into the state file (executor-1/wt-1/state-1) — without this the next
    cycle re-emits a CLOSE for an already-closed position. The pre-fix sweep
    dispatched the close but never wrote state."""
    from agent.sandbox.carry_state import CarryPositionRecord, CarryState
    from agent.sandbox.execute import Action, ActionKind, ActionResult

    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot()
    decision = _decision_clean().model_copy(update={"confidence": 0.5})
    carry_state = CarryState(positions=[
        CarryPositionRecord(
            coin="TON",
            opened_at=datetime.now(UTC),
            target_pick_usd=Decimal("100"),
            spot_qty_base=Decimal("50"),
            perp_qty_base=Decimal("50"),
            mark_price_at_open=Decimal("2.0"),
            spot_order_link_id="x_spot",
            perp_order_link_id="x_perp",
        )
    ])
    wake_events = [{"kind": "carry_liq_close", "coin": "TON",
                    "position_id": "perp:TONUSDT"}]
    close_result = ActionResult(
        action=Action(
            kind=ActionKind.CLOSE_FUNDING_CARRY,
            category="FundingCarry",
            product_id="TONUSDT",
            coin="TON",
            amount=Decimal("100"),
            amount_native=Decimal("50"),
            order_link_id="c-001",
            reason="liq de-risk close",
        ),
        status="ok",
        response={},
        error=None,
        started_at="2026-06-04T00:00:00+00:00",
        finished_at="2026-06-04T00:00:01+00:00",
    )

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: []),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(decision, _stub_usage()))),
        patch("agent.sandbox.loop.write_decision", lambda d, sp, **_kw: tmp_path / "decision.json"),
        patch("agent.sandbox.loop.read_carry_state", lambda *_a, **_kw: carry_state),
        patch("agent.sandbox.loop.execute_actions", AsyncMock(return_value=[close_result])),
        patch("agent.sandbox.loop.write_carry_state") as write_mock,
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=True, yes=True, min_confidence=0.6,
            wake_events=wake_events,
        )

    assert write_mock.called, "sweep must reconcile carry_state after closing carry"
    written = write_mock.call_args[0][0]
    assert written.get("TON") is None  # closed position dropped from state
    assert "carry_state_updated" in outcome.get("stages", [])


@pytest.mark.asyncio
async def test_run_one_cycle_derisk_sweep_skips_carry_without_state(tmp_path: Path) -> None:
    """A `carry_liq_close` for a coin with NO carry record (manual naked
    short) closes nothing here — left to the orphan-perp / LLM path."""
    from agent.sandbox.carry_state import CarryState
    from agent.sandbox.execute import ActionKind

    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot()
    decision = _decision_clean().model_copy(update={"confidence": 0.5})
    wake_events = [{"kind": "carry_liq_close", "coin": "SOL",
                    "position_id": "perp:SOLUSDT"}]

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: []),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(decision, _stub_usage()))),
        patch("agent.sandbox.loop.write_decision", lambda d, sp, **_kw: tmp_path / "decision.json"),
        patch("agent.sandbox.loop.read_carry_state", lambda *_a, **_kw: CarryState()),
        patch("agent.sandbox.loop.execute_actions", AsyncMock(return_value=[])) as exec_mock,
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        await run_one_cycle(
            bybit, anthropic_client, live=True, yes=True, min_confidence=0.6,
            wake_events=wake_events,
        )

    assert not [
        c for c in exec_mock.call_args_list
        if c.kwargs.get("dry_run") is False
        and any(a.kind == ActionKind.CLOSE_FUNDING_CARRY for a in c.args[1])
    ]


@pytest.mark.asyncio
async def test_run_one_cycle_carry_liq_close_strips_decision_on_autoclose(
    tmp_path: Path,
) -> None:
    """The bug (loop-1/wt-2): a `carry_liq_close` co-occurring with a
    `pick_invalidated` took the auto-close fast-path, whose valid confidence-1.0
    decision skipped the de-risk sweep — leaving the near-liq carry open. The
    decision-strip drops the carry pre-diff so the executing auto-close cycle
    CLOSEs (and can't re-OPEN) it."""
    from agent.sandbox.carry_state import CarryState

    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot_with_ton(liquid_usdc="100")
    snap.products["FundingCarry"] = [
        ProductSummary(
            category="FundingCarry", product_id="HYPEUSDT", coin="HYPE",
            effective_apr=Decimal("0.08"), apr_source="funding_carry",
            base_apr_string=None, redeem_lockup_minutes=0, notes=[],
        ),
    ]
    # Prior holds an OnChain earn pick (dropped by pick_invalidated → auto-close
    # path) AND a HYPE funding-carry (kept — carry has no family to drop).
    prior = {
        "thesis": "hold a TON OnChain pick and a HYPE funding-carry.",
        "venues": [
            {"venue_id": "cash_usdc", "weight": 0.5, "picks": []},
            {"venue_id": "bybit_onchain", "weight": 0.35,
             "picks": [{"product_id": "8", "weight": 1.0}]},
            {"venue_id": "bybit_funding_carry", "weight": 0.15,
             "picks": [{"product_id": "HYPEUSDT", "weight": 1.0}]},
        ],
        "hedges": [], "confidence": 0.7, "risk_flags": [], "notes": [],
        "expected_blended_apr_pct": 5.0,
    }
    wake_events = [
        {"kind": "pick_invalidated", "position_id": "earn:8", "coin": "TON"},
        {"kind": "carry_liq_close", "coin": "HYPE", "position_id": "perp:HYPEUSDT"},
    ]

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: [prior]),
        patch("agent.sandbox.loop.write_decision", lambda d, sp, **_kw: tmp_path / "decision.json"),
        patch("agent.sandbox.loop.read_carry_state", lambda *_a, **_kw: CarryState()),
        patch("agent.sandbox.loop.execute_actions", AsyncMock(return_value=[])),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=True, yes=True, min_confidence=0.6,
            wake_events=wake_events,
        )

    # Auto-close path taken (pick_invalidated) AND the carry was stripped so the
    # diff can't re-open it.
    assert outcome.get("auto_close") is True
    assert outcome.get("carry_liq_close_dropped") == ["HYPEUSDT"]


@pytest.mark.asyncio
async def test_run_one_cycle_no_actions_when_book_zero(tmp_path: Path) -> None:
    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot(total_equity_usd="0")
    decision = _decision_clean()

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(decision, _stub_usage()))),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp, **_kw: tmp_path / "decision.json",
        ),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=False, yes=False, min_confidence=0.6
        )

    assert outcome["result"] == "no_actions"
    assert outcome["actions_planned"] == 0


@pytest.mark.asyncio
async def test_run_one_cycle_halts_when_marker_present(tmp_path: Path) -> None:
    """Operator places `state/HALT` → cycle short-circuits before
    snapshot. No Bybit API call, no decision, no execute. Outcome's
    `result="halted"` carries the reason from the file so the cycle
    log shows WHY we stopped without grepping the log."""
    from agent.sandbox.safety import halt
    halt("operator paused for review")

    bybit = AsyncMock()
    anthropic_client = AsyncMock()

    with patch(
        "agent.sandbox.loop.collect_snapshot",
        AsyncMock(),
    ) as snap_mock:
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=True, yes=True, min_confidence=0.6
        )

    assert outcome["result"] == "halted"
    assert outcome.get("halt_reason") is not None
    assert "operator paused" in outcome["halt_reason"]
    snap_mock.assert_not_called()
    assert "finished_at" in outcome


@pytest.mark.asyncio
async def test_run_one_cycle_trips_halt_on_24h_drawdown(tmp_path: Path) -> None:
    """24h-old equity at $400, current at $300 = 25% drop → exceeds
    default 10% threshold → cycle creates HALT marker, returns with
    `halt_trigger="daily_drawdown"`, and does NOT reach decision/execute."""
    from datetime import timedelta
    from agent.sandbox.safety import EQUITY_HISTORY_FILE, HALT_FILE, record_equity

    # Seed history with a 25h-old high-water entry.
    record_equity(
        Decimal("400"),
        ts=datetime.now(UTC) - timedelta(hours=25),
    )

    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot(total_equity_usd="300")  # 25% drop vs $400 baseline

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch(
            "agent.sandbox.loop.write_snapshot",
            lambda s: tmp_path / "snap.json",
        ),
        patch("agent.sandbox.loop.decide", AsyncMock()) as decide_mock,
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=False, yes=False, min_confidence=0.6
        )

    assert outcome["result"] == "halted"
    assert outcome.get("halt_trigger") == "daily_drawdown"
    assert "drawdown" in outcome["halt_reason"]
    # HALT marker on disk so the NEXT cycle also short-circuits.
    assert HALT_FILE.exists()
    # decide() was never called — circuit broke before LLM round-trip.
    decide_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_one_cycle_trips_halt_when_carry_state_write_fails(
    tmp_path: Path,
) -> None:
    """State-coherence guard: if write_carry_state raises after a
    successful execute, the cycle creates HALT so the operator must
    manually reconcile before the next run (otherwise a stale state
    file could lead to a double-position next cycle). We mock around
    the diff/execute layer here — the carry-state pathway is exercised
    by injecting a state mutation that triggers the write."""
    from agent.sandbox.carry_state import CarryPositionRecord, CarryState
    from agent.sandbox.execute import ActionKind
    from agent.sandbox.execute import Action, ActionResult
    from agent.sandbox.safety import HALT_FILE

    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot()
    decision = _decision_clean()

    # Fake "successful carry open" result that apply_carry_results_to_state
    # turns into a non-empty state, so write_carry_state actually runs.
    carry_action = Action(
        kind=ActionKind.OPEN_FUNDING_CARRY,
        category="FundingCarry",
        product_id="TONUSDT",
        coin="TON",
        amount=Decimal("15"),
        amount_native=Decimal("7.5"),
        order_link_id="t-001",
        reason="carry open",
        extra={"mark_price": "2.0"},
    )
    fake_result = ActionResult(
        action=carry_action,
        status="ok",
        response={"legs": {"spot": {}, "perp": {}}},
        error=None,
        started_at="2026-06-04T00:00:00+00:00",
        finished_at="2026-06-04T00:00:01+00:00",
    )

    fresh_state = CarryState(
        positions=[
            CarryPositionRecord(
                coin="TON",
                opened_at=datetime.now(UTC),
                target_pick_usd=Decimal("15"),
                spot_qty_base=Decimal("7.5"),
                perp_qty_base=Decimal("7.5"),
                mark_price_at_open=Decimal("2.0"),
                spot_order_link_id="t-001_spot",
                perp_order_link_id="t-001_perp",
            )
        ]
    )

    boom = OSError("disk full")
    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch(
            "agent.sandbox.loop.write_snapshot",
            lambda s: tmp_path / "snap.json",
        ),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(decision, _stub_usage()))),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp, **_kw: tmp_path / "decision.json",
        ),
        patch(
            "agent.sandbox.loop.request_approval", return_value=True
        ),
        # Bypass real diff/execute — they're heavy and orthogonal to the
        # carry-state failure path under test.
        patch(
            "agent.sandbox.loop.diff_to_actions",
            return_value=[carry_action],
        ),
        patch(
            "agent.sandbox.loop.execute_actions",
            AsyncMock(return_value=[fake_result]),
        ),
        patch(
            "agent.sandbox.loop.apply_carry_results_to_state",
            return_value=fresh_state,
        ),
        # The bug class we're guarding: state write raises after execute.
        patch("agent.sandbox.loop.write_carry_state", side_effect=boom),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=True, yes=True, min_confidence=0.6
        )

    # Cycle still completes (execute already happened) but flags the
    # carry_state_error AND auto-creates HALT so the next cycle stops.
    assert outcome.get("carry_state_error") is not None
    assert "disk full" in outcome["carry_state_error"]
    assert HALT_FILE.exists()
    body = HALT_FILE.read_text()
    assert "carry_state write failed" in body


@pytest.mark.asyncio
async def test_run_one_cycle_swallows_snapshot_exception(tmp_path: Path) -> None:
    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    boom = RuntimeError("Bybit auth blew up")

    with patch(
        "agent.sandbox.loop.collect_snapshot",
        AsyncMock(side_effect=boom),
    ):
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=False, yes=False, min_confidence=0.6
        )

    assert outcome["result"] == "error"
    assert "Bybit auth blew up" in outcome["error"]
    # Cycle log entry is still well-formed (started_at + finished_at + error).
    assert "started_at" in outcome and "finished_at" in outcome


@pytest.mark.asyncio
async def test_run_one_cycle_live_without_approval_downgrades(tmp_path: Path) -> None:
    bybit = AsyncMock()
    bybit.place_earn_order = AsyncMock(return_value=EarnOrderResult(orderId="x"))
    anthropic_client = AsyncMock()
    snap = _snapshot()
    decision = _decision_clean()

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(decision, _stub_usage()))),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp, **_kw: tmp_path / "decision.json",
        ),
        patch(
            "agent.sandbox.loop.request_approval",
            return_value=False,  # operator declines
        ),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=True, yes=False, min_confidence=0.6
        )

    # Approval declined ⇒ downgrade to dry-run ⇒ no live API calls.
    assert outcome["approved"] is False
    assert outcome["result"] == "ok"  # not "executed"
    bybit.place_earn_order.assert_not_called()


def _ok_probe() -> dict[str, str]:
    """All probe endpoints green — used by run_loop tests that aren't
    testing the probe itself."""
    return {
        "wallet_balance[UNIFIED]": "ok",
        "list_earn_products[FlexibleSaving]": "ok",
        "list_earn_products[OnChain]": "ok",
        "earn_positions[FlexibleSaving]": "ok",
        "lm_products": "ok",
        "advance_products[DualAssets]": "ok",
        "tickers_linear": "ok",
    }


@pytest.mark.asyncio
async def test_run_loop_once_executes_single_cycle(tmp_path: Path) -> None:
    log_path = tmp_path / "cycle_log.jsonl"
    snap = _snapshot()
    decision = _decision_clean()

    # Patch the cheap surfaces. Anthropic/Bybit clients are opened
    # inside run_loop via context managers — patch the `from_settings`
    # constructor + AsyncAnthropic to return AsyncMocks.
    anthropic_client = AsyncMock()
    anthropic_client.__aenter__.return_value = anthropic_client
    bybit_client = AsyncMock()
    bybit_client.__aenter__.return_value = bybit_client
    bybit_client.permission_probe = AsyncMock(return_value=_ok_probe())

    with (
        patch(
            "agent.sandbox.loop.anthropic.AsyncAnthropic",
            return_value=anthropic_client,
        ),
        patch(
            "agent.sandbox.loop.BybitClient.from_settings",
            return_value=bybit_client,
        ),
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(decision, _stub_usage()))),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp, **_kw: tmp_path / "decision.json",
        ),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        await run_loop(
            interval_seconds=60.0,
            live=False,
            yes=False,
            min_confidence=0.6,
            once=True,
            cycle_log_path=log_path,
        )

    assert log_path.is_file()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["result"] in ("ok", "no_actions")


@pytest.mark.asyncio
async def test_run_loop_honors_stop_event(tmp_path: Path) -> None:
    """Setting `stop_event` before `run_loop` starts → zero cycles run."""
    log_path = tmp_path / "cycle_log.jsonl"
    stop = asyncio.Event()
    stop.set()  # pre-set so the while predicate is false on first check

    bybit_client = AsyncMock()
    bybit_client.__aenter__.return_value = bybit_client
    bybit_client.permission_probe = AsyncMock(return_value=_ok_probe())
    anthropic_client = AsyncMock()
    anthropic_client.__aenter__.return_value = anthropic_client

    with (
        patch(
            "agent.sandbox.loop.anthropic.AsyncAnthropic",
            return_value=anthropic_client,
        ),
        patch(
            "agent.sandbox.loop.BybitClient.from_settings",
            return_value=bybit_client,
        ),
    ):
        await run_loop(
            interval_seconds=60.0,
            live=False,
            yes=False,
            min_confidence=0.6,
            once=False,
            cycle_log_path=log_path,
            stop_event=stop,
        )

    assert not log_path.exists() or log_path.read_text().strip() == ""


@pytest.mark.asyncio
async def test_run_loop_aborts_on_critical_permission_denied(tmp_path: Path) -> None:
    """Probe says wallet_balance is denied → loop refuses to start."""
    log_path = tmp_path / "cycle_log.jsonl"
    bybit_client = AsyncMock()
    bybit_client.__aenter__.return_value = bybit_client
    denied_probe = dict(_ok_probe())
    denied_probe["wallet_balance[UNIFIED]"] = "permission_denied"
    bybit_client.permission_probe = AsyncMock(return_value=denied_probe)
    anthropic_client = AsyncMock()
    anthropic_client.__aenter__.return_value = anthropic_client

    with (
        patch(
            "agent.sandbox.loop.anthropic.AsyncAnthropic",
            return_value=anthropic_client,
        ),
        patch(
            "agent.sandbox.loop.BybitClient.from_settings",
            return_value=bybit_client,
        ),pytest.raises(SystemExit) as excinfo
    ):
        await run_loop(
            interval_seconds=60.0,
            live=False,
            yes=False,
            min_confidence=0.6,
            once=True,
            cycle_log_path=log_path,
        )
    assert "wallet_balance" in str(excinfo.value)
    # No cycle should have run — log either absent or empty.
    assert not log_path.exists() or log_path.read_text().strip() == ""


@pytest.mark.asyncio
async def test_run_loop_continues_on_informational_probe_failure(tmp_path: Path) -> None:
    """LM / advance / linear probes failing is a warning, not abort."""
    log_path = tmp_path / "cycle_log.jsonl"
    snap = _snapshot()
    decision = _decision_clean()

    bybit_client = AsyncMock()
    bybit_client.__aenter__.return_value = bybit_client
    warn_probe = dict(_ok_probe())
    warn_probe["lm_products"] = "permission_denied"
    warn_probe["advance_products[DualAssets]"] = "error:180001"
    bybit_client.permission_probe = AsyncMock(return_value=warn_probe)
    anthropic_client = AsyncMock()
    anthropic_client.__aenter__.return_value = anthropic_client

    with (
        patch(
            "agent.sandbox.loop.anthropic.AsyncAnthropic",
            return_value=anthropic_client,
        ),
        patch(
            "agent.sandbox.loop.BybitClient.from_settings",
            return_value=bybit_client,
        ),
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(decision, _stub_usage()))),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp, **_kw: tmp_path / "decision.json",
        ),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        # Should NOT raise — informational failures just log warnings.
        await run_loop(
            interval_seconds=60.0,
            live=False,
            yes=False,
            min_confidence=0.6,
            once=True,
            cycle_log_path=log_path,
        )
    assert log_path.is_file()
    assert len(log_path.read_text().strip().splitlines()) == 1


# ───────────────── event-driven-rebalance.3 plumbing ──────────────────


def test_format_wake_events_renders_severity_kind_message() -> None:
    from agent.sandbox.decide import _format_wake_events

    events = [
        {"severity": "P0", "kind": "price_drift",
         "message": "TON mark drifted -7.30%"},
        {"severity": "P0", "kind": "funding_flip",
         "message": "TON funding flipped 0.0005 → -0.0003"},
    ]
    out = _format_wake_events(events)
    assert out.startswith("## Wake reason")
    assert "[P0 price_drift] TON mark drifted -7.30%" in out
    assert "[P0 funding_flip] TON funding flipped" in out


def test_build_user_message_includes_wake_section_first() -> None:
    """When wake_events present, the section is the FIRST block of the
    user message — Claude reads it before the snapshot JSON."""
    from agent.sandbox.decide import _build_user_message

    events = [
        {"severity": "P0", "kind": "price_drift", "message": "TON -7%"}
    ]
    msg = _build_user_message({"foo": "bar"}, wake_events=events)
    wake_idx = msg.find("## Wake reason")
    allocate_idx = msg.find("Allocate the vault")
    assert wake_idx == 0
    assert wake_idx < allocate_idx
    assert "[P0 price_drift] TON -7%" in msg


def test_build_user_message_no_wake_section_when_empty() -> None:
    from agent.sandbox.decide import _build_user_message

    msg_none = _build_user_message({"foo": "bar"}, wake_events=None)
    msg_empty = _build_user_message({"foo": "bar"}, wake_events=[])
    assert "## Wake reason" not in msg_none
    assert "## Wake reason" not in msg_empty
    # Standard prompt still comes first
    assert msg_none.startswith("Allocate the vault")
    assert msg_empty.startswith("Allocate the vault")


def test_write_decision_persists_wake_events(tmp_path: Path) -> None:
    """write_decision stamps wake_events + wake_reason into `_meta` so
    `.8` cost tracking can attribute the cycle."""
    from agent.sandbox.decide import write_decision

    decision = _decision_clean()
    snap_path = tmp_path / "snap.json"
    snap_path.write_text("{}")
    events = [
        {"kind": "price_drift", "severity": "P0", "message": "x"},
        {"kind": "funding_flip", "severity": "P0", "message": "y"},
    ]
    out = write_decision(
        decision,
        snap_path,
        decisions_dir=tmp_path,
        wake_events=events,
    )
    payload = json.loads(out.read_text())
    assert payload["_meta"]["wake_events"] == events
    assert payload["_meta"]["wake_reason"] == "event:funding_flip,price_drift"


def test_write_decision_defaults_wake_reason_heartbeat(tmp_path: Path) -> None:
    from agent.sandbox.decide import write_decision

    decision = _decision_clean()
    snap_path = tmp_path / "snap.json"
    snap_path.write_text("{}")
    out = write_decision(decision, snap_path, decisions_dir=tmp_path)
    payload = json.loads(out.read_text())
    assert payload["_meta"]["wake_reason"] == "heartbeat"
    assert "wake_events" not in payload["_meta"]


@pytest.mark.asyncio
async def test_run_one_cycle_stamps_wake_reason_heartbeat(tmp_path: Path) -> None:
    """Default cycle (no wake_events) → wake_reason='heartbeat' in outcome."""
    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot()
    decision = _decision_clean()
    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(decision, _stub_usage()))),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp, **_kw: tmp_path / "decision.json",
        ),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=False, yes=False, min_confidence=0.6
        )
    assert outcome["wake_reason"] == "heartbeat"


@pytest.mark.asyncio
async def test_run_one_cycle_passes_wake_events_through(tmp_path: Path) -> None:
    """wake_events passed in → decide() called with them + outcome
    wake_reason="event:price_drift"."""
    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot()
    decision = _decision_clean()
    decide_mock = AsyncMock(return_value=(decision, _stub_usage()))
    write_decision_mock = MagicMock(
        side_effect=lambda d, sp, **_kw: tmp_path / "decision.json"
    )

    fake_events = [
        {"kind": "price_drift", "severity": "P0", "message": "TON -7%"}
    ]
    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: None),
        patch("agent.sandbox.loop.decide", decide_mock),
        patch(
            "agent.sandbox.loop.write_decision",
            write_decision_mock,
        ),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit,
            anthropic_client,
            live=False,
            yes=False,
            min_confidence=0.6,
            wake_events=fake_events,
        )
    assert outcome["wake_reason"] == "event:price_drift"
    # decide called with wake_events kwarg
    assert decide_mock.call_args.kwargs.get("wake_events") == fake_events
    # write_decision called with wake_events kwarg
    assert write_decision_mock.call_args.kwargs.get("wake_events") == fake_events


@pytest.mark.asyncio
async def test_run_one_cycle_updates_watcher_baseline(tmp_path: Path) -> None:
    """run_one_cycle MUST call update_baseline_from_snapshot after the
    snapshot writes, even when validator later rejects. Critical for
    keeping the watcher in sync with real Bybit holdings (`.3` design).
    """
    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot()
    bad_decision = _decision_with_risk_flag()
    baseline_path = tmp_path / "baseline.json"
    baseline_mock = MagicMock(side_effect=lambda *a, **kw: None)

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(bad_decision, _stub_usage()))),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp, **_kw: tmp_path / "decision.json",
        ),
        patch("agent.sandbox.loop.update_baseline_from_snapshot", baseline_mock),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"earn_positions": []}))
        outcome = await run_one_cycle(
            bybit,
            anthropic_client,
            live=False,
            yes=False,
            min_confidence=0.6,
            watcher_baseline_path=baseline_path,
        )
    assert outcome["result"] == "skipped:invalid"
    # Baseline updated even on rejection
    baseline_mock.assert_called_once()
    assert baseline_mock.call_args.kwargs["path"] == baseline_path


@pytest.mark.asyncio
async def test_run_loop_watcher_wakes_early_on_p0_event(tmp_path: Path) -> None:
    """With --enable-watcher, the watcher task setting wake_event short-
    circuits the inter-cycle sleep and a second cycle fires within ms,
    NOT after `interval_seconds`."""
    from agent.sandbox.watcher import EventRecord

    log_path = tmp_path / "cycle_log.jsonl"
    snap = _snapshot()
    decision = _decision_clean()

    bybit_client = AsyncMock()
    bybit_client.__aenter__.return_value = bybit_client
    bybit_client.permission_probe = AsyncMock(return_value=_ok_probe())
    anthropic_client = AsyncMock()
    anthropic_client.__aenter__.return_value = anthropic_client

    # Watcher fakery: first poll returns one P0 event then stops firing
    poll_calls = {"n": 0}

    async def _fake_poll(_client, _baseline):
        poll_calls["n"] += 1
        if poll_calls["n"] == 1:
            return [
                EventRecord(
                    ts=datetime.now(UTC),
                    kind="price_drift",
                    severity="P0",
                    coin="TON",
                    message="TON drifted",
                )
            ]
        return []

    # Stop after the second cycle finishes — set stop_event from inside
    # `decide` so we have deterministic control.
    cycles = {"n": 0}
    stop_event = asyncio.Event()

    async def _decide(*_a, **_kw):
        cycles["n"] += 1
        if cycles["n"] >= 2:
            stop_event.set()
        return decision, _stub_usage()

    with (
        patch(
            "agent.sandbox.loop.anthropic.AsyncAnthropic",
            return_value=anthropic_client,
        ),
        patch(
            "agent.sandbox.loop.BybitClient.from_settings",
            return_value=bybit_client,
        ),
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: None),
        patch("agent.sandbox.loop.decide", _decide),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp, **_kw: tmp_path / "decision.json",
        ),
        patch("agent.sandbox.loop.watcher_poll_once", _fake_poll),
        patch(
            "agent.sandbox.loop.read_watcher_baseline",
            lambda _p: __import__(
                "agent.sandbox.watcher", fromlist=["WatcherBaseline"]
            ).WatcherBaseline(captured_at=datetime.now(UTC)),
        ),
        patch("agent.sandbox.loop.write_watcher_events", lambda *a, **kw: None),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        # interval_seconds=60 is intentional — if the wake path failed,
        # this test would hang for 60s. Pytest's default timeout would
        # then kill it. With wake_event firing, second cycle should
        # start within ~100ms of first cycle finishing.
        await asyncio.wait_for(
            run_loop(
                interval_seconds=60.0,
                live=False,
                yes=False,
                min_confidence=0.6,
                once=False,
                cycle_log_path=log_path,
                stop_event=stop_event,
                enable_watcher=True,
                watcher_interval_seconds=0.01,
                watcher_baseline_path=tmp_path / "baseline.json",
                watcher_events_dir=tmp_path / "events",
            ),
            timeout=5.0,
        )

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) >= 2, "watcher wake should have driven a second cycle"
    # Second cycle's wake_reason reflects the event
    second = json.loads(lines[1])
    assert second["wake_reason"].startswith("event:")


@pytest.mark.asyncio
async def test_run_loop_watcher_disabled_by_default(tmp_path: Path) -> None:
    """Without --enable-watcher, no watcher task spawns: a wake_event
    set externally has no observable effect on cadence."""
    log_path = tmp_path / "cycle_log.jsonl"
    snap = _snapshot()
    decision = _decision_clean()

    bybit_client = AsyncMock()
    bybit_client.__aenter__.return_value = bybit_client
    bybit_client.permission_probe = AsyncMock(return_value=_ok_probe())
    anthropic_client = AsyncMock()
    anthropic_client.__aenter__.return_value = anthropic_client

    poll_calls = {"n": 0}

    async def _fake_poll(_client, _baseline):
        poll_calls["n"] += 1
        return []

    with (
        patch(
            "agent.sandbox.loop.anthropic.AsyncAnthropic",
            return_value=anthropic_client,
        ),
        patch(
            "agent.sandbox.loop.BybitClient.from_settings",
            return_value=bybit_client,
        ),
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(decision, _stub_usage()))),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp, **_kw: tmp_path / "decision.json",
        ),
        patch("agent.sandbox.loop.watcher_poll_once", _fake_poll),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        await run_loop(
            interval_seconds=60.0,
            live=False,
            yes=False,
            min_confidence=0.6,
            once=True,
            cycle_log_path=log_path,
            # default enable_watcher=False
        )
    # Watcher should NOT have polled even once
    assert poll_calls["n"] == 0


# ─────────── event-driven-rebalance.7 — end-to-end integration ────────


@pytest.mark.asyncio
async def test_e2e_price_drop_drives_event_driven_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: baseline has a TON perp at $1.78 → tickers come back
    at $1.65 → real `watcher_poll_once` detects price_drift (P0) →
    `wake_event` set → main loop wakes early → second cycle's outcome
    carries `wake_reason="event:price_drift"` and `decide()` got the
    event payload.

    Differs from `.3` test_run_loop_watcher_wakes_early_on_p0_event:
    that one stubs `watcher_poll_once` directly. This one exercises the
    full path through the actual watcher logic — checker functions,
    ticker fan-out, event emission to JSONL.
    """
    from agent.sandbox import watcher as watcher_module
    from agent.sandbox.watcher import HeldPosition, WatcherBaseline, write_baseline

    log_path = tmp_path / "cycle_log.jsonl"
    baseline_path = tmp_path / "watcher-baseline.json"
    events_dir = tmp_path / "events"

    # Seed baseline ON DISK before run_loop starts — the watcher reads
    # from this path on every poll.
    write_baseline(
        WatcherBaseline(
            captured_at=datetime.now(UTC),
            positions=[
                HeldPosition(
                    position_id="perp:TONUSDT",
                    venue="perp",
                    coin="TON",
                    entry_mark_price=Decimal("1.78"),
                    last_funding_rate=Decimal("0.0002"),
                )
            ],
            known_h2e_product_ids=[],
        ),
        baseline_path,
    )

    snap = _snapshot()
    decision = _decision_clean()

    class _FakeTicker:
        # `poll_once` reads `t.symbol` via getattr() THEN falls back to
        # `t.model_dump()` for the rest. Need both surfaces.
        def __init__(self, symbol: str, mark: str, funding: str):
            self.symbol = symbol
            self.markPrice = mark
            self.fundingRate = funding

        def model_dump(self) -> dict[str, str]:
            return {
                "symbol": self.symbol,
                "markPrice": self.markPrice,
                "fundingRate": self.fundingRate,
            }

    bybit_client = AsyncMock()
    bybit_client.__aenter__.return_value = bybit_client
    bybit_client.permission_probe = AsyncMock(return_value=_ok_probe())
    # Ticker fan-out: mark dropped from 1.78 → 1.65 (-7.3% > 5% threshold);
    # funding unchanged so only price_drift fires.
    bybit_client.get_tickers = AsyncMock(
        return_value=[_FakeTicker("TONUSDT", "1.65", "0.0002")]
    )
    bybit_client.list_hold_to_earn_products = AsyncMock(return_value=[])
    anthropic_client = AsyncMock()
    anthropic_client.__aenter__.return_value = anthropic_client

    # Stop after the second cycle fires — captured via decide hook.
    cycles = {"n": 0, "calls": []}
    stop_event = asyncio.Event()

    async def _decide(*_a, **kw):
        cycles["n"] += 1
        cycles["calls"].append(kw.get("wake_events"))
        if cycles["n"] >= 2:
            stop_event.set()
        return decision, _stub_usage()

    # No-op peg fetch so we don't spam CoinGecko in CI and so peg_drift
    # doesn't also fire and mask the assertions.
    async def _peg_stub() -> Decimal:
        return Decimal("1.0")

    monkeypatch.setattr(watcher_module, "_fetch_peg_usd", _peg_stub)

    with (
        patch(
            "agent.sandbox.loop.anthropic.AsyncAnthropic",
            return_value=anthropic_client,
        ),
        patch(
            "agent.sandbox.loop.BybitClient.from_settings",
            return_value=bybit_client,
        ),
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: None),
        patch("agent.sandbox.loop.decide", _decide),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp, **_kw: tmp_path / "decision.json",
        ),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        await asyncio.wait_for(
            run_loop(
                interval_seconds=60.0,
                live=False,
                yes=False,
                min_confidence=0.6,
                once=False,
                cycle_log_path=log_path,
                stop_event=stop_event,
                enable_watcher=True,
                watcher_interval_seconds=0.01,
                watcher_baseline_path=baseline_path,
                watcher_events_dir=events_dir,
            ),
            timeout=5.0,
        )

    # ── Assertions on the cycle log ────────────────────────────────
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) >= 2, "watcher wake should have driven a second cycle"
    second = json.loads(lines[1])
    assert second["wake_reason"] == "event:price_drift", (
        f"expected event-driven second cycle, got {second.get('wake_reason')!r}"
    )

    # ── decide() received the wake_events ─────────────────────────
    # First call = heartbeat (None); second = wake (non-empty list with
    # price_drift kind).
    second_call_events = cycles["calls"][1]
    assert second_call_events, "decide() did not receive wake_events on cycle 2"
    kinds = {e.get("kind") for e in second_call_events}
    assert "price_drift" in kinds

    # ── Event was persisted to JSONL ──────────────────────────────
    jsonl_files = list(events_dir.glob("*.jsonl"))
    assert jsonl_files, "watcher did not write any event JSONL"
    raw_events = jsonl_files[0].read_text().strip().splitlines()
    assert raw_events
    parsed = json.loads(raw_events[0])
    assert parsed["kind"] == "price_drift"
    assert parsed["severity"] == "P0"
    assert parsed["coin"] == "TON"


# ─────────── data-store.9 — DB writer failure isolation ────────────────


@pytest.mark.asyncio
async def test_run_loop_continues_when_db_record_cycle_raises(
    tmp_path: Path,
) -> None:
    """If the cycle store throws (Postgres down, schema mismatch, etc.)
    the file-based path MUST stay intact: cycle_log.jsonl still gets
    the row, the loop does not crash. Files are source of truth; DB
    is a derived view.

    Patches `_record_cycle_from_outcome` to raise on first call so we
    don't need a real Postgres fixture — the contract being tested is
    the run_loop try/except, not the writer."""
    log_path = tmp_path / "cycle_log.jsonl"
    snap = _snapshot()
    decision = _decision_clean()

    bybit_client = AsyncMock()
    bybit_client.__aenter__.return_value = bybit_client
    bybit_client.permission_probe = AsyncMock(return_value=_ok_probe())
    anthropic_client = AsyncMock()
    anthropic_client.__aenter__.return_value = anthropic_client

    # Pretend the pool is live (truthy) so the `if store_pool is not None`
    # branch runs and our patched record_cycle_from_outcome fires.
    fake_pool = MagicMock()

    record_calls = {"n": 0}

    async def _exploding_record(*_a, **_kw):
        record_calls["n"] += 1
        raise RuntimeError("simulated DB outage")

    with (
        patch(
            "agent.sandbox.loop.anthropic.AsyncAnthropic",
            return_value=anthropic_client,
        ),
        patch(
            "agent.sandbox.loop.BybitClient.from_settings",
            return_value=bybit_client,
        ),
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=(decision, _stub_usage()))),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp, **_kw: tmp_path / "decision.json",
        ),
        patch("agent.sandbox.loop._record_cycle_from_outcome", _exploding_record),
        # Inject the fake pool by intercepting open_pool so the
        # `if enable_store` branch in run_loop produces a non-None
        # store_pool without needing a real DB.
        patch(
            "agent.sandbox.loop.open_pool",
            lambda *_a, **_kw: _async_cm_yielding(fake_pool),
        ),
        patch(
            "agent.sandbox.loop.apply_migrations",
            AsyncMock(return_value=[]),
        ),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        await run_loop(
            interval_seconds=60.0,
            live=False,
            yes=False,
            min_confidence=0.6,
            once=True,
            cycle_log_path=log_path,
            enable_store=True,
            database_url="postgres://fake/none",
        )

    # The DB raised → but cycle still ran + cycle_log written
    assert record_calls["n"] == 1
    assert log_path.is_file()
    line = log_path.read_text().strip().splitlines()[0]
    entry = json.loads(line)
    assert entry["result"] in ("ok", "no_actions")


def _async_cm_yielding(value):
    """Tiny helper: async context manager that yields a fixed value.
    Used to mock `open_pool` without spinning up a real Postgres."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        yield value

    return _cm()


# ─── Auto-close fast-path (2026-06-03) ─────────────────────────────────────

def test_build_auto_close_decision_returns_none_without_prior():
    from agent.sandbox.loop import _build_auto_close_decision
    assert _build_auto_close_decision(None, [{"kind": "pick_invalidated", "position_id": "earn:8"}]) is None


def test_build_auto_close_decision_returns_none_without_invalidate_event():
    """Other event kinds (price_drift, funding_flip) still go through LLM."""
    from agent.sandbox.loop import _build_auto_close_decision
    prior = {
        "venues": [
            {"venue_id": "cash_usdc", "weight": 0.5, "picks": []},
            {"venue_id": "bybit_onchain", "weight": 0.5, "picks": [
                {"product_id": "8", "weight": 1.0},
            ]},
        ],
        "confidence": 0.7,
    }
    events = [{"kind": "price_drift", "position_id": "perp:TONUSDT", "coin": "TON"}]
    assert _build_auto_close_decision(prior, events) is None


def test_build_auto_close_decision_drops_affected_pick_to_cash():
    """Single-pick venue with the affected product gets dropped entirely;
    its weight rolls into cash_usdc."""
    from agent.sandbox.loop import _build_auto_close_decision
    prior = {
        "venues": [
            {"venue_id": "cash_usdc", "weight": 0.3, "picks": []},
            {"venue_id": "bybit_flex", "weight": 0.5, "picks": [
                {"product_id": "1131", "weight": 1.0},
            ]},
            {"venue_id": "bybit_onchain", "weight": 0.2, "picks": [
                {"product_id": "8", "weight": 1.0},
            ]},
        ],
        "hedges": [{"coin": "TON", "notional_usd": -16.0}],
        "confidence": 0.7,
    }
    events = [
        {"kind": "pick_invalidated", "position_id": "earn:8", "coin": "TON"}
    ]
    out = _build_auto_close_decision(prior, events)
    assert out is not None
    venues_by_id = {v["venue_id"]: v for v in out["venues"]}
    # bybit_onchain dropped, its 0.2 in cash now.
    assert "bybit_onchain" not in venues_by_id
    assert venues_by_id["cash_usdc"]["weight"] == pytest.approx(0.5)
    assert venues_by_id["bybit_flex"]["weight"] == 0.5
    # Hedges array cleared so diff_to_actions auto-closes the perp.
    assert out["hedges"] == []
    # Validates as a Decision.
    from agent.reason.schema import Decision
    d = Decision.model_validate(out)
    assert d.confidence == 1.0


def test_build_auto_close_decision_drops_lm_pick_to_cash():
    """An `lm:<productId>` event drops the matching bybit_lm pick (→ diff
    auto-redeems the LP) exactly like an earn pick."""
    from agent.sandbox.loop import _build_auto_close_decision
    prior = {
        "venues": [
            {"venue_id": "cash_usdc", "weight": 0.7, "picks": []},
            {"venue_id": "bybit_lm", "weight": 0.3, "picks": [
                {"product_id": "24", "weight": 1.0},
            ]},
        ],
        "hedges": [{"coin": "ETH", "notional_usd": -150.0}],
        "confidence": 0.7,
    }
    events = [
        {"kind": "pick_invalidated", "position_id": "lm:24", "coin": "ETH"}
    ]
    out = _build_auto_close_decision(prior, events)
    assert out is not None
    venues_by_id = {v["venue_id"]: v for v in out["venues"]}
    assert "bybit_lm" not in venues_by_id
    assert venues_by_id["cash_usdc"]["weight"] == pytest.approx(1.0)
    assert out["hedges"] == []
    from agent.reason.schema import Decision
    Decision.model_validate(out)


def test_build_auto_close_decision_qualifies_drop_by_venue_family():
    """earn and lm productId spaces overlap (loop-2): an `lm:14` event drops
    the bybit_lm pick ONLY — a same-id earn pick survives; `earn:14` is the
    mirror (drops the earn pick, leaves the lm one)."""
    from agent.sandbox.loop import _build_auto_close_decision
    prior = {
        "venues": [
            {"venue_id": "cash_usdc", "weight": 0.4, "picks": []},
            {"venue_id": "bybit_flex", "weight": 0.3, "picks": [
                {"product_id": "14", "weight": 1.0},
            ]},
            {"venue_id": "bybit_lm", "weight": 0.3, "picks": [
                {"product_id": "14", "weight": 1.0},
            ]},
        ],
        "hedges": [{"coin": "ETH", "notional_usd": -150.0}],
        "confidence": 0.7,
    }
    lm_out = _build_auto_close_decision(
        prior, [{"kind": "pick_invalidated", "position_id": "lm:14", "coin": "ETH"}]
    )
    lm_by_id = {v["venue_id"]: v for v in lm_out["venues"]}
    assert "bybit_lm" not in lm_by_id
    assert lm_by_id["bybit_flex"]["weight"] == 0.3  # same pid, other family kept
    assert lm_by_id["bybit_flex"]["picks"][0]["product_id"] == "14"
    assert lm_by_id["cash_usdc"]["weight"] == pytest.approx(0.7)

    earn_out = _build_auto_close_decision(
        prior, [{"kind": "pick_invalidated", "position_id": "earn:14", "coin": "ETH"}]
    )
    earn_by_id = {v["venue_id"]: v for v in earn_out["venues"]}
    assert "bybit_flex" not in earn_by_id
    assert earn_by_id["bybit_lm"]["weight"] == 0.3
    assert earn_by_id["cash_usdc"]["weight"] == pytest.approx(0.7)


def test_build_auto_close_decision_suppresses_recently_closed_pid():
    """A pid already auto-closed within the cooldown window is skipped so a
    persistently-firing event falls through to the normal path (loop-4)."""
    from agent.sandbox.loop import _build_auto_close_decision
    prior = {
        "venues": [
            {"venue_id": "cash_usdc", "weight": 0.7, "picks": []},
            {"venue_id": "bybit_onchain", "weight": 0.3, "picks": [
                {"product_id": "8", "weight": 1.0},
            ]},
        ],
        "confidence": 0.7,
    }
    events = [{"kind": "pick_invalidated", "position_id": "earn:8", "coin": "TON"}]
    assert _build_auto_close_decision(prior, events) is not None
    assert _build_auto_close_decision(prior, events, frozenset({"8"})) is None


def test_build_auto_close_decision_ignores_carry_liq_close():
    """`carry_liq_close` is not a pick_invalidated and carries no
    earn:/lm: pid, so it yields no close_pids → falls through to None
    (the loop's de-risk sweep handles carry, not the decision rewrite)."""
    from agent.sandbox.loop import _build_auto_close_decision
    prior = {
        "venues": [
            {"venue_id": "cash_usdc", "weight": 0.9, "picks": []},
            {"venue_id": "bybit_funding_carry", "weight": 0.1, "picks": [
                {"product_id": "SOLUSDT", "weight": 1.0},
            ]},
        ],
        "confidence": 0.7,
    }
    events = [
        {"kind": "carry_liq_close", "position_id": "perp:SOLUSDT", "coin": "SOL"}
    ]
    assert _build_auto_close_decision(prior, events) is None


def test_build_auto_close_decision_rescales_multi_pick_venue():
    """Venue with two picks: closing one rescales remaining to sum=1 within
    venue + shrinks venue weight proportionally; freed weight to cash."""
    from agent.sandbox.loop import _build_auto_close_decision
    prior = {
        "venues": [
            {"venue_id": "cash_usdc", "weight": 0.1, "picks": []},
            {"venue_id": "bybit_flex", "weight": 0.6, "picks": [
                {"product_id": "1131", "weight": 0.7},  # USD1
                {"product_id": "1", "weight": 0.3},     # USDT
            ]},
            {"venue_id": "bybit_onchain", "weight": 0.3, "picks": [
                {"product_id": "8", "weight": 1.0},
            ]},
        ],
        "confidence": 0.7,
    }
    events = [
        {"kind": "pick_invalidated", "position_id": "earn:1131", "coin": "USD1"}
    ]
    out = _build_auto_close_decision(prior, events)
    assert out is not None
    venues_by_id = {v["venue_id"]: v for v in out["venues"]}
    flex = venues_by_id["bybit_flex"]
    # Kept pick "1" was 0.3 / 1.0 → rescales to 1.0 of venue.
    assert len(flex["picks"]) == 1
    assert flex["picks"][0]["product_id"] == "1"
    assert flex["picks"][0]["weight"] == pytest.approx(1.0)
    # Venue weight shrank to 0.6 * 0.3 = 0.18; freed 0.42 went to cash.
    assert flex["weight"] == pytest.approx(0.18)
    assert venues_by_id["cash_usdc"]["weight"] == pytest.approx(0.52)
    # Total still 1.0 (0.52 + 0.18 + 0.30).
    total = sum(v["weight"] for v in out["venues"])
    assert total == pytest.approx(1.0)


def test_build_auto_close_decision_multiple_picks_at_once():
    """Two simultaneous invalidate events close both picks."""
    from agent.sandbox.loop import _build_auto_close_decision
    prior = {
        "venues": [
            {"venue_id": "cash_usdc", "weight": 0.2, "picks": []},
            {"venue_id": "bybit_flex", "weight": 0.5, "picks": [
                {"product_id": "1131", "weight": 1.0},
            ]},
            {"venue_id": "bybit_onchain", "weight": 0.3, "picks": [
                {"product_id": "8", "weight": 1.0},
            ]},
        ],
        "confidence": 0.7,
    }
    events = [
        {"kind": "pick_invalidated", "position_id": "earn:1131", "coin": "USD1"},
        {"kind": "pick_invalidated", "position_id": "earn:8", "coin": "TON"},
    ]
    out = _build_auto_close_decision(prior, events)
    assert out is not None
    venues_by_id = {v["venue_id"]: v for v in out["venues"]}
    # Both venues dropped; all weight in cash.
    assert "bybit_flex" not in venues_by_id
    assert "bybit_onchain" not in venues_by_id
    assert venues_by_id["cash_usdc"]["weight"] == pytest.approx(1.0)


# ─── .42 mid-cycle restart detection ──────────────────────────────────────


def test_detect_unfinished_cycles_clean_dirs(tmp_path: Path) -> None:
    """Both cycle_log + executions empty → no unfinished cycles."""
    from agent.sandbox.loop import detect_unfinished_cycles

    cycle_log = tmp_path / "cycle_log.jsonl"
    executions = tmp_path / "executions"
    executions.mkdir()
    assert detect_unfinished_cycles(cycle_log, executions) == []


def test_detect_unfinished_cycles_all_matched(tmp_path: Path) -> None:
    """Every executions file has a matching cycle_log entry → nothing
    unfinished (canonical clean-restart case)."""
    from agent.sandbox.loop import detect_unfinished_cycles

    ts = "20260604T120000Z"
    executions = tmp_path / "executions"
    executions.mkdir()
    (executions / f"{ts}.jsonl").write_text(
        json.dumps({"action": {"kind": "subscribe_earn"}, "status": "ok"}) + "\n"
    )
    cycle_log = tmp_path / "cycle_log.jsonl"
    cycle_log.write_text(
        json.dumps({"snapshot_filename": f"{ts}.json", "result": "executed"})
        + "\n"
    )
    assert detect_unfinished_cycles(cycle_log, executions) == []


def test_detect_unfinished_cycles_finds_orphan_execution_file(tmp_path: Path) -> None:
    """Crash scenario: executions/<ts>.jsonl exists but no cycle_log
    entry → surface with the reconcile summary."""
    from agent.sandbox.loop import detect_unfinished_cycles

    completed = "20260604T120000Z"
    crashed = "20260604T160000Z"
    executions = tmp_path / "executions"
    executions.mkdir()
    (executions / f"{completed}.jsonl").write_text(
        json.dumps({"action": {"kind": "subscribe_earn"}, "status": "ok"}) + "\n"
    )
    (executions / f"{crashed}.jsonl").write_text(
        "\n".join([
            json.dumps({
                "action": {"kind": "subscribe_earn", "product_id": "p1"},
                "status": "ok",
            }),
            json.dumps({
                "action": {"kind": "swap_spot", "product_id": "ETHUSDC"},
                "status": "error",
                "error": "retCode=170131",
            }),
        ]) + "\n"
    )
    cycle_log = tmp_path / "cycle_log.jsonl"
    cycle_log.write_text(
        json.dumps({
            "snapshot_filename": f"{completed}.json",
            "result": "executed",
        }) + "\n"
    )

    unfinished = detect_unfinished_cycles(cycle_log, executions)
    assert len(unfinished) == 1
    u = unfinished[0]
    assert u["snapshot_ts"] == crashed
    assert u["total"] == 2
    assert u["counts"] == {"ok": 1, "error": 1}


def test_detect_unfinished_cycles_missing_cycle_log(tmp_path: Path) -> None:
    """Brand-new install (no cycle_log yet) but executions exist → all
    count as unfinished."""
    from agent.sandbox.loop import detect_unfinished_cycles

    executions = tmp_path / "executions"
    executions.mkdir()
    (executions / "20260604T120000Z.jsonl").write_text(
        json.dumps({"action": {"kind": "subscribe_earn"}, "status": "ok"}) + "\n"
    )
    cycle_log = tmp_path / "cycle_log.jsonl"  # not on disk
    unfinished = detect_unfinished_cycles(cycle_log, executions)
    assert len(unfinished) == 1
    assert unfinished[0]["snapshot_ts"] == "20260604T120000Z"


def test_detect_unfinished_cycles_skips_empty_execution_files(tmp_path: Path) -> None:
    """Empty `.jsonl` → don't surface; the cycle didn't actually do
    anything that needs reconciliation."""
    from agent.sandbox.loop import detect_unfinished_cycles

    executions = tmp_path / "executions"
    executions.mkdir()
    (executions / "20260604T120000Z.jsonl").write_text("")
    cycle_log = tmp_path / "cycle_log.jsonl"
    cycle_log.write_text("")
    assert detect_unfinished_cycles(cycle_log, executions) == []


# --- agent-yield-quality.4 / .5 : deterministic confidence + expected-APR ---


def _snap_recompute(
    *, total_equity_usd: str = "100",
    flex_apr_source: str = "estimate_apr",
    flex_held: str = "0",
    net_hedge: str | None = None,
    errors: list[str] | None = None,
) -> Snapshot:
    """Snapshot with one NON-STABLE (TON) FlexibleSaving product + paired
    perp, for the confidence / expected-APR recompute tests. `flex_held`
    seeds a held TON Earn position (native) so net-new vs held can be
    exercised; `net_hedge` sets `effective_apr_net_hedge` (fractional) so the
    APR blend can be net-of-hedge."""
    held = (
        [{"productId": "TON1", "coin": "TON", "amount": flex_held,
          "category": "FlexibleSaving", "status": "Active"}]
        if Decimal(flex_held) > 0 else []
    )
    return Snapshot(
        captured_at=datetime.now(UTC),
        wallet=WalletSnapshot(
            total_equity_usd=Decimal(total_equity_usd),
            liquid_usdc_usd=Decimal("80"), liquid_usdt_usd=Decimal("0"),
        ),
        earn_positions=held, lm_positions=[],
        products={
            "FlexibleSaving": [ProductSummary(
                category="FlexibleSaving", product_id="TON1", coin="TON",
                effective_apr=Decimal("0.20"), apr_source=flex_apr_source,
                effective_apr_net_hedge=(
                    Decimal(net_hedge) if net_hedge is not None else None
                ),
                base_apr_string=None, redeem_lockup_minutes=None, notes=[],
            )],
            "OnChain": [], "LiquidityMining": [],
        },
        market=MarketSnapshot(),
        perp_market={"TON": PerpInfo(
            symbol="TONUSDT", mark_price=Decimal("2.0"),
            min_notional_usd=Decimal("1.0"),
            funding_rate_7d_avg=Decimal("0.00005"),  # positive → above floor
            funding_interval_hours=Decimal("8"),
        )},
        usdc_peg=UsdcPegSnapshot(price_usd=Decimal("1.0"), deviation_bps=Decimal("0"),
                                 fetched_at=datetime.now(UTC)),
        errors=errors or [],
    )


def _dec_recompute(
    *, confidence: float = 0.65, flex_weight: float = 0.05,
    expected_apr: float = 9.9,
) -> dict:
    """A decision picking the TON Flex product (small NEW non-stable)."""
    return Decision(
        thesis="probe a fresh non-stable TON Flex pick for the recompute tests.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=1.0 - flex_weight),
            VenueAllocation(venue_id="bybit_flex", weight=flex_weight,
                            picks=[Pick(product_id="TON1", weight=1.0)]),
        ],
        hedges=[], confidence=confidence, risk_flags=[], notes=[],
        expected_blended_apr_pct=expected_apr,
    ).model_dump()


def test_recompute_confidence_penalizes_unconfirmed_estimate_apr() -> None:
    from agent.sandbox.loop import _recompute_confidence
    snap = _snap_recompute()  # TON estimate_apr, NEW ($5 net-new = 5% of $100 book)
    new, reasons = _recompute_confidence(_dec_recompute(confidence=0.65), snap, [])
    # Proportional: 0.10 × (0.05 / 0.30) ≈ 0.0167 → a 5% probe stays ABOVE the
    # 0.60 execute gate (a flat 0.10 used to drop it to 0.55 and block it).
    assert 0.60 <= new < 0.65
    assert abs(new - (0.65 - 0.10 * (0.05 / 0.30))) < 1e-6
    assert any("unconfirmed_apr" in r for r in reasons)


def test_recompute_confidence_unconfirmed_penalty_full_at_large_tilt() -> None:
    """A LARGE NEW unconfirmed tilt (≥ CONF_UNCONFIRMED_FULL_FRAC of book) takes
    the full penalty, unlike a small probe."""
    from agent.sandbox.loop import _recompute_confidence, CONF_PENALTY_UNCONFIRMED_APR
    snap = _snap_recompute()  # $100 book
    # 40% of book into the unconfirmed TON pick → past the 30% full-penalty frac.
    new, reasons = _recompute_confidence(
        _dec_recompute(confidence=0.65, flex_weight=0.40), snap, []
    )
    assert abs(new - (0.65 - CONF_PENALTY_UNCONFIRMED_APR)) < 1e-9
    assert any("unconfirmed_apr" in r for r in reasons)


def test_recompute_confidence_penalizes_snapshot_errors() -> None:
    from agent.sandbox.loop import _recompute_confidence
    # Confirmed APR (no unconfirmed penalty), but a snapshot data gap that
    # touches the PICKED coin (TON perp ticker failed → its hedge is blind).
    snap = _snap_recompute(
        flex_apr_source="apr_history",
        errors=["perp_market[TON]: tickers: BybitAPIError"],
    )
    new, reasons = _recompute_confidence(_dec_recompute(confidence=0.65), snap, [])
    # data_gap −0.10 then all_confirmed bonus +0.05 (the only pick is
    # apr_history); capped at base+bonus, so 0.65 − 0.10 + 0.05 = 0.60.
    assert abs(new - 0.60) < 1e-9
    assert any("data_gap" in r for r in reasons)


def test_recompute_confidence_ignores_unpicked_coin_errors() -> None:
    from agent.sandbox.loop import _recompute_confidence
    # snapshot.errors is a catch-all: a perp ticker for an UNPICKED coin (METH)
    # fails every cycle. It must NOT dock the data-gap penalty, else the 0.65
    # anchor falls below the 0.60 execute gate on essentially every cycle and
    # the agent silently stops trading. The pick (TON) is fully confirmed.
    snap = _snap_recompute(
        flex_apr_source="apr_history",
        errors=[
            "perp_market[METH]: tickers: BybitAPIError",
            "advance_position[DualAssets/136052]: retCode=10006 rate limit",
        ],
    )
    new, reasons = _recompute_confidence(_dec_recompute(confidence=0.65), snap, [])
    # No data_gap; only the all_confirmed +0.05 → 0.70 (capped at base+bonus).
    assert abs(new - 0.70) < 1e-9
    assert not any("data_gap" in r for r in reasons)


def test_recompute_confidence_penalizes_failed_legs_last_cycle() -> None:
    from agent.sandbox.loop import _recompute_confidence
    snap = _snap_recompute(flex_apr_source="apr_history")
    priors = [{"confidence": 0.65,
               "_cycle_outcome": {"result": "executed_partial", "actions_failed": 1}}]
    new, reasons = _recompute_confidence(_dec_recompute(confidence=0.65), snap, priors)
    # failed_legs −0.10 + all_confirmed +0.05, capped at base+0.05 → 0.60.
    assert abs(new - 0.60) < 1e-9
    assert any("failed_legs" in r for r in reasons)


def test_recompute_confidence_bonus_when_all_confirmed_capped() -> None:
    from agent.sandbox.loop import _recompute_confidence
    snap = _snap_recompute(flex_apr_source="measured_yield")
    new, reasons = _recompute_confidence(_dec_recompute(confidence=0.65), snap, [])
    # Only the explicit bonus may RAISE, and only by CONF_BONUS_ALL_CONFIRMED.
    assert abs(new - 0.70) < 1e-9  # 0.65 + 0.05
    assert any("all_confirmed" in r for r in reasons)


def test_recompute_confidence_bonus_cannot_inflate_low_llm_confidence() -> None:
    from agent.sandbox.loop import _recompute_confidence
    # A low LLM confidence with a fully-confirmed book: the bonus is capped at
    # base + bonus, so it can never lift a 0.45 into a live (>=0.60) trade.
    snap = _snap_recompute(flex_apr_source="apr_history")
    new, _ = _recompute_confidence(_dec_recompute(confidence=0.45), snap, [])
    assert abs(new - 0.50) < 1e-9  # 0.45 + 0.05, not pinned up to the floor


def test_recompute_confidence_floors_at_min_when_penalties_stack() -> None:
    from agent.sandbox.loop import _recompute_confidence
    from agent.validate.rules import MIN_CONFIDENCE
    # estimate_apr NEW at a LARGE tilt (40% > 30% full-penalty frac → −0.10)
    # + pick-relevant snapshot error (−0.10) + failed legs (−0.10) + budget
    # starved (−0.05) = −0.35 off 0.65 → 0.30, floored to MIN.
    snap = _snap_recompute(errors=["[TON] bybit 5xx"])
    dec = _dec_recompute(confidence=0.65, flex_weight=0.40)
    dec["_outcome_liquid_clamp_dropped"] = ["TON1"]
    priors = [{"confidence": 0.65,
               "_cycle_outcome": {"result": "error", "actions_failed": 0}}]
    new, reasons = _recompute_confidence(dec, snap, priors)
    assert abs(new - MIN_CONFIDENCE) < 1e-9
    assert any("budget_starved" in r for r in reasons)


def test_recompute_confidence_noop_when_clean() -> None:
    from agent.sandbox.loop import _recompute_confidence
    # Held TON (no NEW spend) on a confirmed source, no errors, clean prior —
    # the only penalty/bonus is gated off, so confidence is unchanged.
    snap = _snap_recompute(flex_apr_source="apr_history", flex_held="50")
    # flex 0.05 → target $5 < held $100, net_new < 0 → not NEW. But the bonus
    # WOULD raise (all confirmed). Use a single held confirmed pick → bonus
    # fires; to test a true no-op, drop confidence so base+bonus == base is
    # impossible — instead assert it only moves by the bonus.
    new, reasons = _recompute_confidence(
        _dec_recompute(confidence=0.65, flex_weight=0.40), snap, []
    )
    # $40 target < $100 held → net_new negative → no unconfirmed penalty even
    # if source were estimate; here source is apr_history so all_confirmed
    # bonus applies (+0.05).
    assert abs(new - 0.70) < 1e-9
    assert reasons == ["all_confirmed (every pick apr_history/measured_yield): +0.05"]


def test_recompute_confidence_truly_noop_returns_base() -> None:
    from agent.sandbox.loop import _recompute_confidence
    # A picks-less (cash-only) decision: no picks at all → no bonus, no
    # penalties → confidence returned unchanged.
    snap = _snap_recompute()
    dec = Decision(
        thesis="cash-only hold with nothing to recompute for the test.",
        venues=[VenueAllocation(venue_id="cash_usdc", weight=1.0)],
        hedges=[], confidence=0.65, risk_flags=[], notes=[],
        expected_blended_apr_pct=0.0,
    ).model_dump()
    new, reasons = _recompute_confidence(dec, snap, [])
    assert new == 0.65
    assert reasons == []


def test_confidence_anchor_warning_fires_on_streak() -> None:
    from agent.sandbox.loop import CONF_ANCHOR_STREAK_N, _confidence_anchor_warning
    # current + (N-1) priors all 0.65 → fires.
    priors = [{"confidence": 0.65} for _ in range(CONF_ANCHOR_STREAK_N - 1)]
    msg = _confidence_anchor_warning(0.65, priors)
    assert msg is not None and "anchored" in msg


def test_confidence_anchor_warning_none_on_differing() -> None:
    from agent.sandbox.loop import CONF_ANCHOR_STREAK_N, _confidence_anchor_warning
    priors = [{"confidence": 0.65} for _ in range(CONF_ANCHOR_STREAK_N - 2)]
    priors.append({"confidence": 0.70})  # breaks the streak
    assert _confidence_anchor_warning(0.65, priors) is None
    # Too few priors → None.
    assert _confidence_anchor_warning(0.65, [{"confidence": 0.65}]) is None


def test_recompute_expected_apr_blends_net_of_hedge() -> None:
    from agent.sandbox.loop import _recompute_expected_apr
    # Non-stable TON gross 20% but net-of-hedge 6% (funding bleed). The blend
    # must use the NET, and cash contributes 0.
    snap = _snap_recompute(net_hedge="0.06")
    dec = _dec_recompute(flex_weight=0.40, expected_apr=8.0)  # LLM said 8%
    apr, breakdown = _recompute_expected_apr(dec, snap)
    # weight_in_book = 0.40 * 1.0; net-of-hedge 6% → 0.40 * 6 = 2.4%.
    assert abs(apr - 2.4) < 1e-9
    assert len(breakdown) == 1
    assert abs(breakdown[0]["pick_apr_pct"] - 6.0) < 1e-9


def test_recompute_expected_apr_stable_uses_effective_apr() -> None:
    from agent.sandbox.loop import _recompute_expected_apr
    # A stable pick uses plain effective_apr (no hedge); cash = 0.
    snap = _snapshot()  # USD1 Flex at 7.52%, stable
    dec = Decision(
        thesis="stable USD1 Flex blend for the expected-apr units check.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=0.6),
            VenueAllocation(venue_id="bybit_flex", weight=0.4,
                            picks=[Pick(product_id="1131", weight=1.0)]),
        ],
        hedges=[], confidence=0.7, risk_flags=[], notes=[],
        expected_blended_apr_pct=4.0,
    ).model_dump()
    apr, _ = _recompute_expected_apr(dec, snap)
    # 0.4 * 7.52% = 3.008% (percent units, matching the schema).
    assert abs(apr - 3.008) < 1e-6


@pytest.mark.asyncio
async def test_run_one_cycle_recomputes_confidence_end_to_end(tmp_path: Path) -> None:
    """LLM stub returns the 0.65 anchor + a NEW non-stable estimate_apr pick
    (5% of book); `run_one_cycle` lowers it to the deterministic recompute
    (proportional unconfirmed penalty ≈ 0.633) and records
    `confidence_recomputed`. The lowered value is what gets persisted."""
    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snap_recompute()  # TON estimate_apr, NEW
    decision = Decision.model_validate(_dec_recompute(confidence=0.65))

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: []),
        patch("agent.sandbox.loop.decide",
              AsyncMock(return_value=(decision, _stub_usage()))),
        patch("agent.sandbox.loop.write_decision",
              lambda d, sp, **_kw: tmp_path / "decision.json"),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=False, yes=False, min_confidence=0.6
        )

    expected = 0.65 - 0.10 * (0.05 / 0.30)  # proportional: 5% probe ≈ 0.6333
    assert abs(outcome["confidence"] - expected) < 1e-6
    assert outcome["confidence_recomputed"]["from"] == 0.65
    assert abs(outcome["confidence_recomputed"]["to"] - expected) < 1e-6


@pytest.mark.asyncio
async def test_run_one_cycle_auto_close_confidence_not_recomputed(tmp_path: Path) -> None:
    """The auto-close fast-path sets confidence=1.0 by design and must stay
    exempt from the recompute (it skips the LLM block entirely)."""
    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snap_recompute()
    # A prior decision holding the TON pick, plus a pick_invalidated wake event
    # → auto-close path builds a deterministic confidence=1.0 close.
    prior = _dec_recompute(confidence=0.65)
    wake = [{"kind": "pick_invalidated", "position_id": "earn:TON1", "coin": "TON"}]

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions",
              lambda *_a, **_kw: [prior]),
        patch("agent.sandbox.loop.write_decision",
              lambda d, sp, **_kw: tmp_path / "decision.json"),
        patch("agent.sandbox.loop.decide",
              AsyncMock(side_effect=AssertionError("LLM must be skipped"))),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=False, yes=False, min_confidence=0.6,
            wake_events=wake,
        )

    assert outcome.get("auto_close") is True
    assert outcome["confidence"] == 1.0  # untouched by the recompute
    assert "confidence_recomputed" not in outcome


@pytest.mark.asyncio
async def test_run_one_cycle_recomputes_expected_apr_end_to_end(tmp_path: Path) -> None:
    """`outcome[expected_apr_pct]` is the deterministic net-of-hedge blend of
    snapshot APRs, not the LLM's hand-computed headline."""
    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    # TON net-of-hedge 6%; held so the pick isn't NEW (keeps the cycle clean of
    # the unconfirmed/probe machinery) and the blend is purely about units.
    snap = _snap_recompute(flex_apr_source="apr_history", net_hedge="0.06",
                           flex_held="50")
    decision = Decision.model_validate(
        _dec_recompute(confidence=0.65, flex_weight=0.40, expected_apr=8.0)
    )

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_recent_prior_decisions", lambda *_a, **_kw: []),
        patch("agent.sandbox.loop.decide",
              AsyncMock(return_value=(decision, _stub_usage()))),
        patch("agent.sandbox.loop.write_decision",
              lambda d, sp, **_kw: tmp_path / "decision.json"),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=False, yes=False, min_confidence=0.6
        )

    # 0.40 * 6% net-of-hedge = 2.4% (not the LLM's 8.0).
    assert abs(outcome["expected_apr_pct"] - 2.4) < 1e-6
    assert abs(outcome["expected_apr_recomputed"]["to"] - 2.4) < 1e-6
    assert outcome["expected_apr_recomputed"]["from"] == 8.0
