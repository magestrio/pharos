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
from unittest.mock import AsyncMock, patch

import pytest

from agent.bybit_oracle.bybit_client import EarnOrderResult
from agent.reason.schema import Decision, Pick, VenueAllocation
from agent.sandbox.loop import run_loop, run_one_cycle
from agent.sandbox.snapshot import (
    MarketSnapshot,
    ProductSummary,
    Snapshot,
    UsdcPegSnapshot,
    WalletSnapshot,
)


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
                    apr_source="promo_whitelist",
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
            VenueAllocation(venue_id="cash_usdc", weight=0.5),
            VenueAllocation(
                venue_id="bybit_flex",
                weight=0.5,
                picks=[Pick(product_id="1131", weight=1.0)],
            ),
        ],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=4.0,
    )


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
            "agent.sandbox.loop._load_latest_prior_decision",
            lambda: None,
        ),
        patch(
            "agent.sandbox.loop.decide",
            AsyncMock(return_value=decision),
        ),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp: tmp_path / "decision.json",
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
        patch("agent.sandbox.loop._load_latest_prior_decision", lambda: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=bad_decision)),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp: tmp_path / "decision.json",
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
async def test_run_one_cycle_no_actions_when_book_zero(tmp_path: Path) -> None:
    bybit = AsyncMock()
    anthropic_client = AsyncMock()
    snap = _snapshot(total_equity_usd="0")
    decision = _decision_clean()

    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_latest_prior_decision", lambda: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=decision)),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp: tmp_path / "decision.json",
        ),
    ):
        (tmp_path / "snap.json").write_text(json.dumps({"foo": "bar"}))
        outcome = await run_one_cycle(
            bybit, anthropic_client, live=False, yes=False, min_confidence=0.6
        )

    assert outcome["result"] == "no_actions"
    assert outcome["actions_planned"] == 0


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
        patch("agent.sandbox.loop._load_latest_prior_decision", lambda: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=decision)),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp: tmp_path / "decision.json",
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
        patch("agent.sandbox.loop._load_latest_prior_decision", lambda: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=decision)),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp: tmp_path / "decision.json",
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
        ),
    ):
        with pytest.raises(SystemExit) as excinfo:
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
        patch("agent.sandbox.loop._load_latest_prior_decision", lambda: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=decision)),
        patch(
            "agent.sandbox.loop.write_decision",
            lambda d, sp: tmp_path / "decision.json",
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
