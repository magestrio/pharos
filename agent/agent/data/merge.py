from datetime import datetime, timedelta, timezone

import pandas as pd

from agent.data.storage import DATA_ROOT, save_parquet

_CUTOFF = datetime.now(tz=timezone.utc) - timedelta(days=90)

_APY_COLS = ["meth_apy", "cmeth_apy", "aave_usdc_apy", "susde_apy"]

_CRITICAL_COLS = [
    "eth_price", "meth_price", "meth_apy", "cmeth_apy",
    "susde_apy", "aave_usdc_apy", "funding_rate_8h", "mantle_tvl",
]


def _load_raw(name: str) -> pd.DataFrame | None:
    path = DATA_ROOT / "raw" / f"{name}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path, engine="pyarrow")
    if df.empty or "timestamp" not in df.columns:
        return None
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def _daily(df: pd.DataFrame, value_col: str, out_col: str) -> pd.Series:
    s = df.set_index("timestamp")[value_col]
    return s.resample("D").last().rename(out_col)


def build_daily_dataset() -> pd.DataFrame:
    eth = _load_raw("coingecko_ethereum")
    meth = _load_raw("coingecko_mantle-staked-ether")
    dex = _load_raw("dexscreener_meth")
    funding = _load_raw("funding_rates")
    tvl_mantle = _load_raw("tvl_mantle")
    tvl_moe = _load_raw("tvl_merchant-moe")
    tvl_lendle = _load_raw("tvl_lendle")
    yields_meth = _load_raw("yields_cmeth")
    yields_aave_usdc = _load_raw("yields_aave_usdc")
    yields_susde = _load_raw("yields_susde")
    allora = _load_raw("allora_synthetic")

    parts = []

    if eth is not None:
        parts.append(_daily(eth, "price_usd", "eth_price"))
    if meth is not None:
        parts.append(_daily(meth, "price_usd", "meth_price"))

    if dex is not None and not dex.empty:
        parts.append(_daily(dex, "price_usd", "meth_eth_dex_price"))

    if eth is not None and meth is not None:
        eth_d = _daily(eth, "price_usd", "eth_price")
        meth_d = _daily(meth, "price_usd", "meth_price")
        parts.append((meth_d / eth_d).rename("meth_exchange_rate"))

    if yields_meth is not None:
        parts.append(_daily(yields_meth, "apy", "meth_apy"))
    if yields_meth is not None:
        parts.append(_daily(yields_meth, "apy", "cmeth_apy"))
    if yields_aave_usdc is not None:
        parts.append(_daily(yields_aave_usdc, "apy", "aave_usdc_apy"))
    if yields_susde is not None:
        parts.append(_daily(yields_susde, "apy", "susde_apy"))

    if funding is not None:
        parts.append(_daily(funding, "funding_rate_8h", "funding_rate_8h"))
        funding_d = funding.set_index("timestamp")["funding_rate_8h"].resample("D").mean()
        parts.append(funding_d.rolling(7, min_periods=1).mean().rename("funding_rate_7d_avg"))

    if tvl_mantle is not None:
        parts.append(_daily(tvl_mantle, "tvl_usd", "mantle_tvl"))
    if tvl_moe is not None:
        parts.append(_daily(tvl_moe, "tvl_usd", "merchant_moe_tvl"))
    if tvl_lendle is not None:
        parts.append(_daily(tvl_lendle, "tvl_usd", "lendle_tvl"))

    if allora is not None:
        parts.append(_daily(allora, "network_inference", "allora_eth_7d"))
        is_synth = allora.set_index("timestamp")["is_synthetic"].resample("D").last().rename("is_synthetic")
        parts.append(is_synth)

    df = pd.concat(parts, axis=1).sort_index()

    # Snapshot which APY cells are NaN before any filling
    flag_cols = []
    for col in _APY_COLS:
        if col in df.columns:
            flag = f"{col}_is_extrapolated"
            df[flag] = df[col].isna()
            flag_cols.append(flag)

    # Fill APY gaps: forward-fill then back-fill (covers tail and head gaps)
    for col in _APY_COLS:
        if col in df.columns:
            df[col] = df[col].ffill().bfill()

    df = df[df.index >= _CUTOFF]
    df.index.name = "date"
    df = df.reset_index()

    # --- Full dataset ---
    save_parquet(df, "daily_90d", "processed")
    print(f"[merge] full dataset:  {df.shape[0]} rows, {df.shape[1]} cols → daily_90d.parquet")

    # Extrapolation summary
    extrap_counts = {col.replace("_is_extrapolated", ""): int(df[col].sum()) for col in flag_cols}
    print("[merge] extrapolated cells in full:")
    for name, count in extrap_counts.items():
        print(f"  {name}: {count} rows")

    # --- Clean dataset ---
    apy_flags_present = [fc for fc in flag_cols if fc in df.columns]
    no_extrap = ~df[apy_flags_present].any(axis=1) if apy_flags_present else pd.Series(True, index=df.index)
    non_apy_critical = [c for c in ["eth_price", "meth_price", "funding_rate_8h", "mantle_tvl"] if c in df.columns]
    no_nan_critical = df[non_apy_critical].notna().all(axis=1)
    is_clean = no_extrap & no_nan_critical

    if is_clean.any():
        first_clean = df.loc[is_clean, "date"].min()
        clean_df = df[df["date"] >= first_clean].drop(columns=flag_cols).copy()
        save_parquet(clean_df, "daily_clean", "processed")
        print(
            f"[merge] clean dataset: {clean_df.shape[0]} rows "
            f"({first_clean.date()} → {clean_df['date'].max().date()}), "
            f"{clean_df.shape[1]} cols → daily_clean.parquet"
        )
    else:
        print("[merge] WARNING: no fully-clean rows found, daily_clean not saved")

    return df
