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
from agent.sandbox.loop import run_loop, run_one_cycle
from agent.sandbox.snapshot import (
    MarketSnapshot,
    ProductSummary,
    Snapshot,
    UsdcPegSnapshot,
    WalletSnapshot,
)


@pytest.fixture(autouse=True)
def _stub_watcher_baseline_update():
    """`event-driven-rebalance.3` plumbed an `update_baseline_from_snapshot`
    call inside `run_one_cycle` (writes `state/watcher-baseline.json`).
    Stub it out across this file so existing cycle/loop tests don't
    pollute the on-disk state."""
    with patch(
        "agent.sandbox.loop.update_baseline_from_snapshot",
        lambda *_a, **_kw: None,
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
        patch("agent.sandbox.loop._load_latest_prior_decision", lambda: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=bad_decision)),
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
        patch("agent.sandbox.loop._load_latest_prior_decision", lambda: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=decision)),
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
        patch("agent.sandbox.loop._load_latest_prior_decision", lambda: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=decision)),
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
        patch("agent.sandbox.loop._load_latest_prior_decision", lambda: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=decision)),
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
    decide_mock = AsyncMock(return_value=decision)
    write_decision_mock = MagicMock(
        side_effect=lambda d, sp, **_kw: tmp_path / "decision.json"
    )

    fake_events = [
        {"kind": "price_drift", "severity": "P0", "message": "TON -7%"}
    ]
    with (
        patch("agent.sandbox.loop.collect_snapshot", AsyncMock(return_value=snap)),
        patch("agent.sandbox.loop.write_snapshot", lambda s: tmp_path / "snap.json"),
        patch("agent.sandbox.loop._load_latest_prior_decision", lambda: None),
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
        patch("agent.sandbox.loop._load_latest_prior_decision", lambda: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=bad_decision)),
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
        return decision

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
        patch("agent.sandbox.loop._load_latest_prior_decision", lambda: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=decision)),
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
        return decision

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
        patch("agent.sandbox.loop._load_latest_prior_decision", lambda: None),
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
        patch("agent.sandbox.loop._load_latest_prior_decision", lambda: None),
        patch("agent.sandbox.loop.decide", AsyncMock(return_value=decision)),
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
