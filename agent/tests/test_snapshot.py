"""Tests for the sandbox snapshot collector.

Focus:
1. Pure helpers (parsing, ranking, summary mapping) — exhaustive.
2. `_safe_earn` — 10005 swallowed + warning, other errors propagate.
3. `collect_snapshot` — end-to-end against an in-memory mock BybitClient
   serving canned fixtures + a patched CoinGecko HTTP layer.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agent.bybit_oracle.bybit_client import (
    BybitAPIError,
    FlexibleEarnProduct,
    LinearTicker,
    OnChainEarnProduct,
    PerpPosition,
)
from agent.sandbox.snapshot import (
    ProductSummary,
    Snapshot,
    UsdcPegSnapshot,
    _flex_or_onchain_summary,
    _is_open_perp,
    _lm_summary,
    _parse_percent,
    _rank,
    _safe_earn,
    _safe_perp_positions,
    collect_snapshot,
)


# ─── _parse_percent ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0.65%", Decimal("0.0065")),
        ("7.52%", Decimal("0.0752")),
        ("3.987471%", Decimal("0.03987471")),
        ("0%", Decimal(0)),
        ("100%", Decimal(1)),
        # Bybit sometimes returns without % (defensive)
        ("0.5", Decimal("0.005")),
    ],
)
def test_parse_percent_normalizes_apr_strings(value, expected):
    assert _parse_percent(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "garbage", "not-a-number%"])
def test_parse_percent_returns_none_for_missing_or_malformed(value):
    assert _parse_percent(value) is None


# ─── _flex_or_onchain_summary ───────────────────────────────────────────────


def _flex(pid: str, apr: str | None, coin: str = "USDC", **extra) -> FlexibleEarnProduct:
    data = {"productId": pid, "coin": coin, "category": "FlexibleSaving", **extra}
    if apr is not None:
        data["estimateApr"] = apr
    return FlexibleEarnProduct.model_validate(data)


def _onchain(
    pid: str, apr: str | None, coin: str = "USDC", **extra
) -> OnChainEarnProduct:
    data = {"productId": pid, "coin": coin, "category": "OnChain", **extra}
    if apr is not None:
        data["estimateApr"] = apr
    return OnChainEarnProduct.model_validate(data)


def test_flex_summary_uses_promo_whitelist_when_hit():
    """USD1 productId=1131 is in promo_whitelist at 7.52% — must
    override the much lower API `estimateApr`."""
    p = _flex("1131", "0.65%", coin="USD1")
    s = _flex_or_onchain_summary(p, "FlexibleSaving")
    assert s.effective_apr == Decimal("0.0752")
    assert s.apr_source == "promo_whitelist"
    assert s.base_apr_string == "0.65%"


def test_flex_summary_falls_back_to_estimate_apr_when_not_whitelisted():
    p = _flex("9999", "1.07%")
    s = _flex_or_onchain_summary(p, "FlexibleSaving")
    assert s.effective_apr == Decimal("0.0107")
    assert s.apr_source == "estimate_apr"


def test_flex_summary_marks_missing_when_no_apr_anywhere():
    p = _flex("9999", None)
    s = _flex_or_onchain_summary(p, "FlexibleSaving")
    assert s.effective_apr == Decimal(0)
    assert s.apr_source == "missing"


def test_flex_summary_normalizes_int_redeem_processing_minute():
    """Regression: Bybit returns redeemProcessingMinute as raw int for
    some products (live-observed in .18 probe)."""
    p = _flex("rpm-int", "1.0%", redeemProcessingMinute=0)
    s = _flex_or_onchain_summary(p, "FlexibleSaving")
    assert s.redeem_lockup_minutes == 0


def test_onchain_summary_surfaces_fixed_term_and_swap_in_notes():
    p = _onchain(
        "lst-cmeth",
        "5.0%",
        coin="ETH",
        duration="Fixed",
        term=30,
        swapCoin="cmETH",
    )
    s = _flex_or_onchain_summary(p, "OnChain")
    assert "fixed_term_days=30" in s.notes
    assert "swap_to=cmETH" in s.notes


# ─── _lm_summary ────────────────────────────────────────────────────────────


def test_lm_summary_converts_apy_e8_to_fractional():
    """apyE8 = 1433162 → 0.01433162 = 1.43% per .24 capture (BTC/USDT)."""
    p = {
        "productId": "1",
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "apyE8": "1433162",
        "maxLeverage": 10,
    }
    s = _lm_summary(p)
    assert s.category == "LiquidityMining"
    assert s.coin == "BTC/USDT"
    assert s.effective_apr == Decimal("0.01433162")
    assert s.apr_source == "apy_e8"
    assert "max_leverage=10" in s.notes


def test_lm_summary_handles_missing_apy_e8():
    p = {"productId": "x", "baseCoin": "FOO", "quoteCoin": "BAR"}
    s = _lm_summary(p)
    assert s.effective_apr == Decimal(0)
    # apyE8 default "0" parses cleanly, so this is apy_e8 not missing —
    # the "missing" branch is only for non-numeric / invalid values
    assert s.apr_source == "apy_e8"


# ─── _rank ──────────────────────────────────────────────────────────────────


def _summary(apr: str) -> ProductSummary:
    return ProductSummary(
        category="FlexibleSaving",
        product_id=apr,  # use apr as id for traceability in assertions
        coin="USDC",
        effective_apr=Decimal(apr),
        apr_source="estimate_apr",
    )


def test_rank_sorts_descending_by_effective_apr():
    items = [_summary("0.01"), _summary("0.05"), _summary("0.02")]
    ranked = _rank(items, top_k=10)
    assert [p.product_id for p in ranked] == ["0.05", "0.02", "0.01"]


def test_rank_caps_at_top_k():
    items = [_summary(f"0.0{i}") for i in range(1, 8)]
    ranked = _rank(items, top_k=3)
    assert len(ranked) == 3
    # top 3 = 0.07, 0.06, 0.05
    assert [p.product_id for p in ranked] == ["0.07", "0.06", "0.05"]


# ─── _safe_earn ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_safe_earn_swallows_10005_and_warns():
    async def coro():
        raise BybitAPIError(10005, "Permission denied", "/v5/earn/position")

    errors: list[str] = []
    result = await _safe_earn(coro(), errors, "earn_positions", default=[])
    assert result == []
    assert len(errors) == 1
    assert "earn_positions" in errors[0]
    assert "Earn permission denied" in errors[0]


@pytest.mark.asyncio
async def test_safe_earn_propagates_other_bybit_errors():
    """Non-10005 errors are real bugs / outages — must NOT be hidden.
    Otherwise a transient 500 looks like an empty position list to the
    LLM and we silently allocate against stale state."""

    async def coro():
        raise BybitAPIError(180001, "Invalid parameter", "/v5/earn/whatever")

    errors: list[str] = []
    with pytest.raises(BybitAPIError):
        await _safe_earn(coro(), errors, "whatever", default=[])
    assert errors == []


@pytest.mark.asyncio
async def test_safe_earn_returns_value_on_success():
    async def coro():
        return ["pos-1"]

    errors: list[str] = []
    result = await _safe_earn(coro(), errors, "ok", default=[])
    assert result == ["pos-1"]
    assert errors == []


# ─── collect_snapshot (integration) ─────────────────────────────────────────


def _mock_client_full() -> AsyncMock:
    client = AsyncMock()
    client.get_asset_overview.return_value = {
        "totalEquity": "9.97",
        "list": [
            {
                "accountType": "UnifiedTradingAccount",
                "totalEquity": "9.97",
                "coinDetail": [{"coin": "USDC", "equity": "9.97"}],
            }
        ],
    }
    client.list_earn_products.side_effect = lambda category, **_: (
        [
            _flex("2", "1.07%", coin="USDC"),
            _flex("1131", "0.65%", coin="USD1"),  # promo whitelist target
        ]
        if category == "FlexibleSaving"
        else [_onchain("12", "3.75%", coin="USDC", swapCoin="USDE")]
    )
    client.list_liquidity_mining_products.return_value = [
        {
            "productId": "1",
            "baseCoin": "BTC",
            "quoteCoin": "USDT",
            "apyE8": "1433162",
            "maxLeverage": 10,
        }
    ]
    # Advance-Earn families default to empty for the legacy shape test —
    # surfacing is exercised in a dedicated test below.
    client.list_advance_earn_products.return_value = []
    client.get_tickers.side_effect = lambda category, symbol: (
        [
            LinearTicker(
                symbol=symbol,
                lastPrice="68000",
                fundingRate="0.0001",
                price24hPcnt="0.015",
            )
        ]
        if symbol == "BTCUSDT"
        else [
            LinearTicker(
                symbol=symbol,
                lastPrice="3500",
                fundingRate="0.00005",
                price24hPcnt="-0.005",
            )
        ]
    )
    client.get_earn_positions.side_effect = BybitAPIError(
        10005, "Permission denied", "/v5/earn/position"
    )
    client.get_liquidity_mining_positions.side_effect = BybitAPIError(
        10005, "Permission denied", "/v5/earn/liquidity-mining/position"
    )
    # `.32`: perp positions default to empty so the snapshot collector
    # has something iterable to consume. Per-test overrides patch this.
    client.get_positions.return_value = []
    return client


async def _fake_peg_ok() -> UsdcPegSnapshot:
    from datetime import UTC, datetime

    return UsdcPegSnapshot(
        price_usd=Decimal("0.9998"),
        deviation_bps=Decimal("-2.0000"),
        fetched_at=datetime.now(UTC),
    )


async def _fake_peg_fail() -> UsdcPegSnapshot:
    from datetime import UTC, datetime

    return UsdcPegSnapshot(
        price_usd=None, deviation_bps=None, fetched_at=datetime.now(UTC)
    )


@pytest.mark.asyncio
async def test_collect_snapshot_full_shape_and_promo_override():
    client = _mock_client_full()

    with patch("agent.sandbox.snapshot._fetch_usdc_peg", _fake_peg_ok):
        snap = await collect_snapshot(client)

    assert isinstance(snap, Snapshot)
    assert snap.schema_version == 1
    # Wallet
    assert snap.wallet.total_equity_usd == Decimal("9.97")
    assert snap.wallet.accounts[0]["accountType"] == "UnifiedTradingAccount"
    # Positions (10005 swallowed, warnings recorded)
    assert snap.earn_positions == []
    assert snap.lm_positions == []
    # 3 entries: earn_positions[FlexibleSaving], earn_positions[OnChain], lm_positions
    assert len(snap.errors) == 3
    assert all("Earn permission denied" in e for e in snap.errors)
    # Products: per-category, ranked, USD1 uses promo whitelist
    assert set(snap.products) == {"FlexibleSaving", "OnChain", "LiquidityMining"}
    flex = snap.products["FlexibleSaving"]
    usd1 = next(p for p in flex if p.product_id == "1131")
    assert usd1.effective_apr == Decimal("0.0752")
    assert usd1.apr_source == "promo_whitelist"
    # USD1 (7.52% promo) ranks above USDC product 2 (1.07%)
    assert flex[0].product_id == "1131"
    # OnChain summary carries swap_to note
    onchain = snap.products["OnChain"]
    assert onchain[0].notes == ["swap_to=USDE"]
    # LM: apy_e8 path
    lm = snap.products["LiquidityMining"]
    assert lm[0].coin == "BTC/USDT"
    assert lm[0].apr_source == "apy_e8"
    # Market
    assert snap.market.btc_price == Decimal("68000")
    assert snap.market.btc_24h_change_pct == Decimal("1.500")
    assert snap.market.btc_funding_rate == Decimal("0.0001")
    assert snap.market.eth_price == Decimal("3500")
    assert snap.market.eth_24h_change_pct == Decimal("-0.500")
    # USDC peg
    assert snap.usdc_peg.price_usd == Decimal("0.9998")
    assert snap.usdc_peg.deviation_bps == Decimal("-2.0000")


@pytest.mark.asyncio
async def test_collect_snapshot_serializes_to_valid_json():
    """Snapshot must round-trip through model_dump_json so the writer
    + downstream readers (Phase B decide.py) don't blow up on Decimal /
    datetime serialization."""
    client = _mock_client_full()

    with patch("agent.sandbox.snapshot._fetch_usdc_peg", _fake_peg_ok):
        snap = await collect_snapshot(client)

    blob = snap.model_dump_json()
    # Round-trip back through Pydantic to prove the JSON is self-describing.
    reparsed = Snapshot.model_validate_json(blob)
    assert reparsed.wallet.total_equity_usd == snap.wallet.total_equity_usd
    assert reparsed.products["FlexibleSaving"][0].product_id == "1131"


@pytest.mark.asyncio
async def test_collect_snapshot_handles_coingecko_failure_softly():
    """Network error to CoinGecko must NOT crash the snapshot — peg
    block goes null, fetched_at still set, other sources unaffected.
    `_fetch_usdc_peg` swallows the error and returns nulls — verified
    here with a stubbed `_fake_peg_fail`."""
    client = _mock_client_full()

    with patch("agent.sandbox.snapshot._fetch_usdc_peg", _fake_peg_fail):
        snap = await collect_snapshot(client)

    assert snap.usdc_peg.price_usd is None
    assert snap.usdc_peg.deviation_bps is None
    assert snap.usdc_peg.fetched_at is not None
    # Other sources unaffected
    assert snap.wallet.total_equity_usd == Decimal("9.97")
    assert snap.market.btc_price == Decimal("68000")


@pytest.mark.asyncio
async def test_fetch_usdc_peg_real_helper_swallows_network_error():
    """Direct test of `_fetch_usdc_peg` fail-soft: HTTP layer raises,
    helper returns nulls instead of propagating. Patches the
    AsyncClient with a MockTransport that always errors."""
    from agent.sandbox.snapshot import _fetch_usdc_peg

    def _err(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    transport = httpx.MockTransport(_err)
    original = httpx.AsyncClient

    def _patched(**kwargs):  # ignore caller's kwargs, use our transport
        return original(transport=transport, timeout=kwargs.get("timeout", 5))

    with patch("agent.sandbox.snapshot.httpx.AsyncClient", _patched):
        peg = await _fetch_usdc_peg()

    assert peg.price_usd is None
    assert peg.deviation_bps is None
    assert peg.fetched_at is not None


@pytest.mark.asyncio
async def test_fetch_usdc_peg_real_helper_parses_success():
    from agent.sandbox.snapshot import _fetch_usdc_peg

    def _ok(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"usd-coin": {"usd": 1.0003}})

    transport = httpx.MockTransport(_ok)
    original = httpx.AsyncClient

    def _patched(**kwargs):
        return original(transport=transport, timeout=kwargs.get("timeout", 5))

    with patch("agent.sandbox.snapshot.httpx.AsyncClient", _patched):
        peg = await _fetch_usdc_peg()

    assert peg.price_usd == Decimal("1.0003")
    assert peg.deviation_bps == Decimal("3.0000")


# ─── perp_positions collector (.32) ────────────────────────────────────────


def _open_short(coin: str, size: str, position_value: str) -> PerpPosition:
    return PerpPosition(
        symbol=f"{coin}USDT",
        side="Sell",
        size=size,
        positionValue=position_value,
        avgPrice="1.0",
        markPrice="1.0",
    )


def test_is_open_perp_filters_flat_and_none_side() -> None:
    assert _is_open_perp(_open_short("TON", "25", "50"))
    assert not _is_open_perp(PerpPosition(symbol="TONUSDT", side="None", size="0"))
    assert not _is_open_perp(PerpPosition(symbol="TONUSDT", side="Sell", size="0"))
    assert not _is_open_perp(
        PerpPosition(symbol="TONUSDT", side="Sell", size="not-a-decimal")
    )


@pytest.mark.asyncio
async def test_safe_perp_positions_swallows_bybit_error() -> None:
    async def boom():
        raise BybitAPIError(10005, "perp permission denied", "/v5/position/list")

    errors: list[str] = []
    out = await _safe_perp_positions(boom(), errors, "perp_positions[linear]")
    assert out == []
    assert len(errors) == 1
    assert "retCode=10005" in errors[0]


@pytest.mark.asyncio
async def test_safe_perp_positions_returns_value_on_success() -> None:
    pos = _open_short("TON", "25", "50")

    async def ok():
        return [pos]

    errors: list[str] = []
    out = await _safe_perp_positions(ok(), errors, "perp_positions[linear]")
    assert out == [pos]
    assert errors == []


@pytest.mark.asyncio
async def test_collect_snapshot_populates_perp_positions_and_filters_flat() -> None:
    client = _mock_client_full()
    client.get_positions.return_value = [
        _open_short("TON", "25", "50"),
        _open_short("DOGE", "1000", "200"),
        # Flat row Bybit echoes for a symbol you've traded recently — must
        # be filtered out so the executor doesn't try to close it.
        PerpPosition(symbol="BTCUSDT", side="None", size="0", positionValue="0"),
    ]

    with patch("agent.sandbox.snapshot._fetch_usdc_peg", _fake_peg_ok):
        snap = await collect_snapshot(client)

    assert len(snap.perp_positions) == 2
    symbols = {p.symbol for p in snap.perp_positions}
    assert symbols == {"TONUSDT", "DOGEUSDT"}


@pytest.mark.asyncio
async def test_collect_snapshot_degrades_when_perp_positions_fail() -> None:
    """A perm/permission error on `/v5/position/list` must NOT crash the
    snapshot — empty list + warning is the contract."""
    client = _mock_client_full()
    client.get_positions.side_effect = BybitAPIError(
        10005, "perp permission denied", "/v5/position/list"
    )

    with patch("agent.sandbox.snapshot._fetch_usdc_peg", _fake_peg_ok):
        snap = await collect_snapshot(client)

    assert snap.perp_positions == []
    assert any("perp_positions" in e for e in snap.errors)
