"""Tests for the sandbox snapshot collector.

Focus:
1. Pure helpers (parsing, ranking, summary mapping) — exhaustive.
2. `_safe_earn` — 10005 swallowed + warning, other errors propagate.
3. `collect_snapshot` — end-to-end against an in-memory mock BybitClient
   serving canned fixtures + a patched CoinGecko HTTP layer.
"""

from __future__ import annotations

from datetime import datetime
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
    ALPHA_MOMENTUM_HAIRCUT,
    MOMENTUM_APR_CAP,
    SMART_LEVERAGE_MOMENTUM_HAIRCUT,
    ProductSummary,
    Snapshot,
    UsdcPegSnapshot,
    _advance_earn_summary,
    _alpha_summary,
    _flex_or_onchain_summary,
    _is_open_perp,
    _kline_period_return,
    _lm_summary,
    _momentum_apr,
    _parse_percent,
    _rank,
    _safe_earn,
    _safe_perp_positions,
    _usdt_in_unified,
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


def test_flex_summary_uses_estimate_apr_from_api():
    """No promo override — `estimateApr` is the single source. USD1's
    UI-only 7.52% promo lives outside the OpenAPI surface and is
    explicitly NOT carried in the snapshot (see `.40` follow-up for
    dynamic promo discovery)."""
    p = _flex("1131", "0.65%", coin="USD1")
    s = _flex_or_onchain_summary(p, "FlexibleSaving")
    assert s.effective_apr == Decimal("0.0065")
    assert s.apr_source == "estimate_apr"
    assert s.base_apr_string == "0.65%"


def test_flex_summary_uses_estimate_apr_for_non_special_product():
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


# ─── _momentum_apr / _kline_period_return (`.55`) ─────────────────────────


def test_momentum_apr_annualizes_period_return():
    """24h move of +0.5% with no haircut, no leverage = 0.005 × 365 = 1.825 raw,
    but clamped to MOMENTUM_APR_CAP = 0.50."""
    apr = _momentum_apr(Decimal("0.005"), 1)
    assert apr == MOMENTUM_APR_CAP


def test_momentum_apr_applies_alpha_haircut_and_clamps():
    """Alpha haircut 0.5. A +20% 24h pump: 0.20 × 365 × 0.5 = 36.5, clamped to 0.50."""
    apr = _momentum_apr(Decimal("0.20"), 1, haircut=ALPHA_MOMENTUM_HAIRCUT)
    assert apr == MOMENTUM_APR_CAP


def test_momentum_apr_short_direction_flips_sign():
    """Up underlying + Short direction = negative APR (LLM should avoid)."""
    apr = _momentum_apr(
        Decimal("0.05"), 7,
        direction_sign=-1,
        haircut=SMART_LEVERAGE_MOMENTUM_HAIRCUT,
    )
    # raw = 0.05 × (365/7) × -1 × 0.3 = -0.7821… clamped to -0.50
    assert apr == -MOMENTUM_APR_CAP


def test_momentum_apr_leverage_multiplies_then_haircut_then_clamp():
    """+1% 7d × 3x leverage × 0.3 haircut = 0.01 × 52.14 × 3 × 0.3 ≈ 0.47 (under cap)."""
    apr = _momentum_apr(
        Decimal("0.01"), 7,
        leverage=3,
        haircut=SMART_LEVERAGE_MOMENTUM_HAIRCUT,
    )
    assert apr is not None
    # under the cap, not at it
    assert apr < MOMENTUM_APR_CAP
    assert apr > Decimal("0.40")


def test_momentum_apr_zero_period_returns_none():
    assert _momentum_apr(Decimal("0.05"), 0) is None
    assert _momentum_apr(Decimal("0.05"), Decimal("-1")) is None


def test_kline_period_return_uses_oldest_open_and_newest_close():
    """Bybit returns most-recent-first. 8 daily candles span a 7d window —
    oldest open ($100) vs newest close ($107) = +7% over 7d."""
    candles = [
        {"open": "107.5", "close": "107"},   # latest (day 7)
        {"open": "106", "close": "107.5"},
        {"open": "105", "close": "106"},
        {"open": "104", "close": "105"},
        {"open": "103", "close": "104"},
        {"open": "102", "close": "103"},
        {"open": "101", "close": "102"},
        {"open": "100", "close": "101"},     # oldest (day 0)
    ]
    period = _kline_period_return(candles)
    assert period == Decimal("0.07")


def test_kline_period_return_handles_empty_window():
    assert _kline_period_return([]) is None
    assert _kline_period_return([{"open": "1", "close": "1"}]) is None


def test_kline_period_return_handles_bad_prices():
    """Non-numeric prices or zero start price → None (fail-soft)."""
    assert _kline_period_return([{"open": "x", "close": "1"}] * 2) is None
    assert _kline_period_return([{"open": "0", "close": "1"}] * 2) is None


# ─── _alpha_summary (`.53` + `.55`) ────────────────────────────────────────


def _biz_token(token_code: str = "DEX_123", symbol: str = "PEPE", **overrides):
    return {
        "tokenCode": token_code,
        "symbol": symbol,
        "chainCode": "ETH",
        "tokenAddress": "0xabc",
        "tokenDecimals": 18,
        "riskFlag": 0,
        "minOrderQuantity": 1,
        "maxOrderQuantity": 50000,
        "payTokenCodes": ["CEX_1", "CEX_2"],
        **overrides,
    }


def test_alpha_summary_returns_none_without_token_code():
    assert _alpha_summary({"symbol": "FOO"}) is None


def test_alpha_summary_missing_when_no_price_info():
    s = _alpha_summary(_biz_token())
    assert s is not None
    assert s.apr_source == "missing"
    assert s.effective_apr == Decimal(0)
    assert s.product_id == "DEX_123"
    assert s.coin == "PEPE"
    assert "chain=ETH" in s.notes
    assert "pay_tokens=CEX_1,CEX_2" in s.notes


def test_alpha_summary_uses_change24h_for_momentum_apr():
    """+10% 24h = 0.10 × 365 × 0.5 = 18.25 → clamped to 0.50."""
    price = {"tokenCode": "DEX_123", "change24h": "0.10", "liquidity": "50000"}
    s = _alpha_summary(_biz_token(), price)
    assert s is not None
    assert s.apr_source == "momentum"
    assert s.effective_apr == MOMENTUM_APR_CAP
    assert s.base_apr_string == "change_24h=0.10"
    assert "change_24h=0.10" in s.notes
    assert "liquidity_usd=50000" in s.notes


def test_alpha_summary_negative_change_yields_negative_capped_apr():
    """-10% 24h = -0.50 (clamped negative cap). LLM should avoid."""
    price = {"tokenCode": "DEX_123", "change24h": "-0.10"}
    s = _alpha_summary(_biz_token(), price)
    assert s is not None
    assert s.apr_source == "momentum"
    assert s.effective_apr == -MOMENTUM_APR_CAP


def test_alpha_summary_flags_risk_warning():
    s = _alpha_summary(_biz_token(riskFlag=1))
    assert s is not None
    assert "risk_flag=warn" in s.notes


# ─── _advance_earn_summary SmartLeverage momentum (`.55`) ──────────────────


def _smart_leverage(
    underlying: str = "BTC", direction: str = "Long", leverage: int = 3, duration: str = "7d"
) -> dict:
    return {
        "productId": "sl-1",
        "underlyingAsset": underlying,
        "direction": direction,
        "leverage": leverage,
        "duration": duration,
        "coin": "USDT",
    }


def test_smart_leverage_missing_when_no_kline_data():
    s = _advance_earn_summary(_smart_leverage(), "SmartLeverage")
    assert s.apr_source == "missing"
    assert s.effective_apr == Decimal(0)


def test_smart_leverage_long_uses_momentum_with_haircut():
    """BTC +2% 7d, Long, 3x leverage: 0.02 × 52.14 × 3 × 0.3 ≈ 0.939 → clamped 0.50."""
    s = _advance_earn_summary(
        _smart_leverage(),
        "SmartLeverage",
        underlying_period_return=Decimal("0.02"),
    )
    assert s.apr_source == "momentum"
    assert s.effective_apr == MOMENTUM_APR_CAP
    assert "underlying_7d=0.02" in s.base_apr_string
    assert "direction=Long" in s.base_apr_string
    assert "leverage=3" in s.base_apr_string


def test_smart_leverage_short_flips_sign():
    """Up underlying + Short = negative APR (correct: shorting an uptrend hurts)."""
    s = _advance_earn_summary(
        _smart_leverage(direction="Short"),
        "SmartLeverage",
        underlying_period_return=Decimal("0.02"),
    )
    assert s.apr_source == "momentum"
    assert s.effective_apr == -MOMENTUM_APR_CAP


def test_smart_leverage_does_not_use_underlying_return_for_other_categories():
    """Passing `underlying_period_return` to non-SmartLeverage is a no-op —
    DualAssets etc. only get their quote-derived APR. Prevents accidental
    misuse of the new kwarg."""
    p = {"productId": "x", "baseCoin": "BTC", "quoteCoin": "USDT", "duration": "7d"}
    s = _advance_earn_summary(
        p, "DualAssets",
        underlying_period_return=Decimal("0.10"),
    )
    assert s.apr_source == "missing"  # no quote was supplied


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
    # 3 Earn entries: FlexibleSaving + OnChain + lm_positions.
    # Plus one on_chain_state warning because Mantle RPC/vault unconfigured.
    earn_errors = [e for e in snap.errors if "Earn permission denied" in e]
    assert len(earn_errors) == 3
    assert any("on_chain_state: skipped" in e for e in snap.errors)
    assert snap.on_chain_state is None
    # Products: per-category, ranked by estimate_apr. No promo override —
    # USD1 surfaces at its raw API APR (0.65%), below USDC's 1.07%.
    assert set(snap.products) == {"FlexibleSaving", "OnChain", "LiquidityMining"}
    flex = snap.products["FlexibleSaving"]
    usd1 = next(p for p in flex if p.product_id == "1131")
    assert usd1.effective_apr == Decimal("0.0065")
    assert usd1.apr_source == "estimate_apr"
    # USDC product 2 (1.07%) ranks above USD1 1131 (0.65%) under clean estimate_apr.
    assert flex[0].product_id == "2"
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
    # USDC product 2 (1.07%) tops the ranking under clean estimate_apr —
    # USD1 1131 (0.65%) is below it after promo override was removed.
    assert reparsed.products["FlexibleSaving"][0].product_id == "2"


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


# ─── USDT margin wallet snapshot (.33) ─────────────────────────────────────


def test_usdt_in_unified_picks_long_form_account_type() -> None:
    """Asset-overview echoes `UnifiedTradingAccount` (long form)."""
    accounts = [
        {
            "accountType": "UnifiedTradingAccount",
            "coinDetail": [
                {"coin": "USDC", "equity": "9.97"},
                {"coin": "USDT", "equity": "15.50"},
            ],
        }
    ]
    assert _usdt_in_unified(accounts) == Decimal("15.50")


def test_usdt_in_unified_picks_short_form_account_type() -> None:
    """Some endpoints return the short `UNIFIED` form — both must work."""
    accounts = [
        {
            "accountType": "UNIFIED",
            "coinDetail": [{"coin": "USDT", "equity": "42"}],
        }
    ]
    assert _usdt_in_unified(accounts) == Decimal("42")


def test_usdt_in_unified_falls_back_to_wallet_balance() -> None:
    """Older captures use `walletBalance` instead of `equity`."""
    accounts = [
        {
            "accountType": "UNIFIED",
            "coinDetail": [{"coin": "USDT", "walletBalance": "8.5"}],
        }
    ]
    assert _usdt_in_unified(accounts) == Decimal("8.5")


def test_usdt_in_unified_returns_zero_when_no_unified_account() -> None:
    accounts = [
        {
            "accountType": "FUND",
            "coinDetail": [{"coin": "USDT", "equity": "100"}],
        }
    ]
    # USDT in FUND is not margin-eligible for linear perps → ignored.
    assert _usdt_in_unified(accounts) == Decimal(0)


def test_usdt_in_unified_returns_zero_when_no_usdt_entry() -> None:
    accounts = [
        {
            "accountType": "UnifiedTradingAccount",
            "coinDetail": [{"coin": "USDC", "equity": "100"}],
        }
    ]
    assert _usdt_in_unified(accounts) == Decimal(0)


@pytest.mark.asyncio
async def test_collect_snapshot_populates_wallet_usdt_available() -> None:
    client = _mock_client_full()
    client.get_asset_overview.return_value = {
        "totalEquity": "25.50",
        "list": [
            {
                "accountType": "UnifiedTradingAccount",
                "totalEquity": "25.50",
                "coinDetail": [
                    {"coin": "USDC", "equity": "10.00"},
                    {"coin": "USDT", "equity": "15.50"},
                ],
            }
        ],
    }

    with patch("agent.sandbox.snapshot._fetch_usdc_peg", _fake_peg_ok):
        snap = await collect_snapshot(client)

    assert snap.wallet.usdt_available_usd == Decimal("15.50")
    assert snap.wallet.total_equity_usd == Decimal("25.50")


# ─── on_chain_state (.37a) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_snapshot_skips_on_chain_when_unconfigured() -> None:
    """Default call (no Mantle RPC/vault args) → on_chain_state stays None,
    warning lands in errors — Bybit half of the snapshot is unaffected."""
    client = _mock_client_full()
    with patch("agent.sandbox.snapshot._fetch_usdc_peg", _fake_peg_ok):
        snap = await collect_snapshot(client)
    assert snap.on_chain_state is None
    assert "AaveV3" not in snap.products
    assert any("on_chain_state: skipped" in e for e in snap.errors)


@pytest.mark.asyncio
async def test_collect_snapshot_populates_on_chain_when_fetch_succeeds() -> None:
    """When the fetcher returns a state, AaveV3 lands in `products` AND
    `on_chain_state` carries the pool details."""
    from agent.sandbox.on_chain import AaveV3UsdcState

    fake_state = AaveV3UsdcState(
        block_number=99_000_000,
        fetched_at=datetime.fromisoformat("2026-05-27T12:00:00+00:00"),
        pool_address="0x458F293454fE0d67EC0655f3672301301DD51422",
        supply_apr=Decimal("0.0421"),
        vault_usdc_micro=12_345_678,  # $12.345678
        vault_ausdc_micro=50_000_000,  # $50.00
    )

    client = _mock_client_full()
    with patch("agent.sandbox.snapshot._fetch_usdc_peg", _fake_peg_ok), patch(
        "agent.sandbox.snapshot._safe_fetch_aave_v3", return_value=fake_state
    ):
        snap = await collect_snapshot(
            client,
            mantle_rpc_url="https://rpc.mantle.xyz",
            mantle_vault_address="0x4dc4a70Ae02d7ca2F3A06b1231b3A9312d82a037",
        )

    # AaveV3 surfaced as a product with apr_source="aave_pool".
    aave = snap.products["AaveV3"]
    assert len(aave) == 1
    assert aave[0].coin == "USDC"
    assert aave[0].effective_apr == Decimal("0.0421")
    assert aave[0].apr_source == "aave_pool"
    assert "pool=0x458F293454fE0d67EC0655f3672301301DD51422" in aave[0].notes

    # on_chain_state mirrors the same numbers in USD-equivalent form.
    assert snap.on_chain_state is not None
    a = snap.on_chain_state.aave_v3_usdc
    assert a is not None
    assert a.supply_apr == Decimal("0.0421")
    assert a.vault_usdc_usd == Decimal("12.345678")
    assert a.vault_ausdc_usd == Decimal("50")
    assert a.block_number == 99_000_000


@pytest.mark.asyncio
async def test_collect_snapshot_degrades_when_on_chain_fetch_fails() -> None:
    """RPC error → state=None, warning in errors, Bybit side unaffected."""
    client = _mock_client_full()
    with patch("agent.sandbox.snapshot._fetch_usdc_peg", _fake_peg_ok), patch(
        "agent.sandbox.snapshot._safe_fetch_aave_v3", return_value=None
    ):
        # _safe_fetch_aave_v3 itself appends the warning in production; the
        # mock skips that, so we just verify on_chain_state stays None and
        # AaveV3 doesn't land in products.
        snap = await collect_snapshot(
            client,
            mantle_rpc_url="https://rpc.mantle.xyz",
            mantle_vault_address="0x4dc4a70Ae02d7ca2F3A06b1231b3A9312d82a037",
        )
    assert snap.on_chain_state is None
    assert "AaveV3" not in snap.products


# ─── advance_earn_quotes persistence (.35) ─────────────────────────────────


@pytest.mark.asyncio
async def test_collect_snapshot_persists_advance_earn_quotes() -> None:
    """Quote responses from `_quote_advance_top_k` must land on the
    snapshot so the executor can build the per-category extra block at
    dispatch time without re-fetching (race against expiry)."""
    client = _mock_client_full()
    # Surface DualAssets + DiscountBuy raw products so they go through
    # the quote fan-out.
    client.list_advance_earn_products.side_effect = lambda category, **_: (
        [{"productId": "da-1", "baseCoin": "BTC", "quoteCoin": "USDT"}]
        if category == "DualAssets"
        else [{"productId": "db-7", "coin": "USDT"}]
        if category == "DiscountBuy"
        else []
    )
    client.get_advance_product_quote.side_effect = lambda category, product_id: (
        {
            "category": "DualAssets",
            "list": [
                {
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "buyLowPrice": [{"selectPrice": "62000", "apyE8": "80000000"}],
                    "expiredTime": "9999999999999",
                }
            ],
        }
        if category == "DualAssets"
        else {
            "category": "DiscountBuy",
            "list": [
                {
                    "coin": "USDT",
                    "instUid": "inst-xyz",
                    "currentPrice": "65000",
                    "purchasePrice": "63000",
                    "knockoutPrice": "55000",
                    "expiredAt": "9999999999999",
                }
            ],
        }
    )

    with patch("agent.sandbox.snapshot._fetch_usdc_peg", _fake_peg_ok):
        snap = await collect_snapshot(client)

    assert "DualAssets/da-1" in snap.advance_earn_quotes
    assert "DiscountBuy/db-7" in snap.advance_earn_quotes
    da = snap.advance_earn_quotes["DualAssets/da-1"]
    assert da["list"][0]["buyLowPrice"][0]["apyE8"] == "80000000"
    # Round-trip through JSON to confirm pydantic doesn't drop the dict.
    blob = snap.model_dump_json()
    reparsed = Snapshot.model_validate_json(blob)
    assert reparsed.advance_earn_quotes["DiscountBuy/db-7"]["list"][0]["instUid"] == "inst-xyz"


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
