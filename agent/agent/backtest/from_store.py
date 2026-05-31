"""Export DB cycle traces → parquet for backtest replay (`data-store.8`).

Companion to `daily_clean.parquet` / `daily_90d.parquet` (market-data
baselines): this script dumps the agent's OWN action history so a
re-tuned prompt / threshold set can be re-scored against real cycles
instead of synthetic fixtures.

Hackathon scope: dump-to-parquet only. Full replay loop (feed the
exported cycles back through `decide()` + score) lands post-deadline.

Output (two files, one parquet each):

- `cycles.parquet` — one row per cycle: cycle_ts, started_at, result,
  wake_reason, confidence, expected_apr_pct, actions_planned/executed,
  validator_ok, thesis, n_venues + the top-3 venues by weight (flat
  columns for easy pandas filtering). Heavyweight JSONB blobs
  (full snapshot, full decision) stay in the DB — load on demand.
- `positions.parquet` — one row per (cycle_ts, venue, product_id):
  coin, amount, amount_usd. Suitable for "what did the agent hold
  on day X" or "position concentration over time".

CLI:
    python -m agent.backtest.from_store --output data/processed/
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pandas as pd

from agent.sandbox.store.pool import open_pool

log = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parents[2].parent / "data" / "processed"
)


def _venue_summary(decision: Any) -> tuple[int, list[str], list[float]]:
    """Pull n_venues + the top-3 venues (id + weight) out of a decision
    payload. Defensive against missing/malformed fields — returns
    sensible empties so the parquet has uniform columns."""
    if not isinstance(decision, dict):
        return 0, [], []
    venues = decision.get("venues") or []
    if not isinstance(venues, list):
        return 0, [], []
    sortable: list[tuple[str, float]] = []
    for v in venues:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("venue_id") or "")
        try:
            w = float(v.get("weight") or 0)
        except (TypeError, ValueError):
            w = 0.0
        if vid:
            sortable.append((vid, w))
    sortable.sort(key=lambda kv: -kv[1])
    top = sortable[:3]
    ids = [t[0] for t in top]
    weights = [t[1] for t in top]
    return len(sortable), ids, weights


async def fetch_cycles_df(
    pool: asyncpg.Pool, *, since: datetime | None = None
) -> pd.DataFrame:
    """Flatten cycles + decisions + a venue summary into one row per
    cycle. Returns a DataFrame ready to write via `to_parquet`.

    Decision JSONB is unpacked into 3 derived columns (thesis,
    n_venues, top venue ids/weights) — the full blob remains in the
    DB for callers that need the rest."""
    sql = """
        SELECT
            c.cycle_ts, c.started_at, c.finished_at,
            c.result, c.wake_reason, c.confidence, c.expected_apr_pct,
            c.actions_planned, c.actions_executed, c.error,
            d.payload AS decision_payload
        FROM cycles c
        LEFT JOIN decisions d USING (cycle_ts)
    """
    params: list[Any] = []
    if since is not None:
        sql += " WHERE c.started_at >= $1"
        params.append(since)
    sql += " ORDER BY c.started_at"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    records: list[dict[str, Any]] = []
    for r in rows:
        decision = r["decision_payload"]
        n_venues, top_ids, top_weights = _venue_summary(decision)
        thesis = ""
        validator_ok: bool | None = None
        if isinstance(decision, dict):
            thesis = str(decision.get("thesis") or "")
            validator = decision.get("_validator") or {}
            if isinstance(validator, dict):
                validator_ok = validator.get("ok") if isinstance(
                    validator.get("ok"), bool
                ) else None
        records.append(
            {
                "cycle_ts": r["cycle_ts"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "result": r["result"],
                "wake_reason": r["wake_reason"],
                "confidence": r["confidence"],
                "expected_apr_pct": r["expected_apr_pct"],
                "actions_planned": r["actions_planned"],
                "actions_executed": r["actions_executed"],
                "validator_ok": validator_ok,
                "error": r["error"],
                "thesis": thesis,
                "n_venues": n_venues,
                "top1_venue": top_ids[0] if len(top_ids) > 0 else None,
                "top1_weight": top_weights[0] if len(top_weights) > 0 else None,
                "top2_venue": top_ids[1] if len(top_ids) > 1 else None,
                "top2_weight": top_weights[1] if len(top_weights) > 1 else None,
                "top3_venue": top_ids[2] if len(top_ids) > 2 else None,
                "top3_weight": top_weights[2] if len(top_weights) > 2 else None,
            }
        )
    return pd.DataFrame.from_records(records)


async def fetch_positions_df(
    pool: asyncpg.Pool, *, since: datetime | None = None
) -> pd.DataFrame:
    """One row per (cycle_ts, venue, product_id). `amount` is cast to
    text in the SQL so pandas keeps it as a string (NUMERIC fidelity);
    cast in user code if you want float arithmetic."""
    sql = """
        SELECT
            p.cycle_ts, p.venue, p.product_id, p.coin,
            p.amount::text AS amount,
            p.amount_usd::text AS amount_usd
        FROM positions_snapshot p
    """
    params: list[Any] = []
    if since is not None:
        sql += (
            " JOIN cycles c USING (cycle_ts) "
            "WHERE c.started_at >= $1"
        )
        params.append(since)
    sql += " ORDER BY p.cycle_ts, p.venue, p.product_id"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return pd.DataFrame.from_records([dict(r) for r in rows])


async def export_to_parquet(
    pool: asyncpg.Pool,
    output_dir: Path,
    *,
    since: datetime | None = None,
    stem: str | None = None,
) -> dict[str, Path]:
    """Write `cycles.parquet` + `positions.parquet` to `output_dir`.
    Returns a dict of file labels → paths actually written. Empty DB
    still emits the files (zero rows) so downstream pipelines have a
    stable artifact path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{stem}" if stem else ""

    cycles_df = await fetch_cycles_df(pool, since=since)
    cycles_path = output_dir / f"cycles{suffix}.parquet"
    cycles_df.to_parquet(cycles_path, index=False)

    positions_df = await fetch_positions_df(pool, since=since)
    positions_path = output_dir / f"positions{suffix}.parquet"
    positions_df.to_parquet(positions_path, index=False)

    log.info(
        "exported %d cycles, %d position rows to %s",
        len(cycles_df), len(positions_df), output_dir,
    )
    return {"cycles": cycles_path, "positions": positions_path}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


async def _async_main(args: argparse.Namespace) -> None:
    async with open_pool(args.database_url) as pool:
        out = await export_to_parquet(
            pool,
            Path(args.output).resolve(),
            since=_parse_iso(args.since),
            stem=args.stem,
        )
    for label, path in out.items():
        print(f"  {label}: {path}")


def _main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Dump agent cycle history from Postgres to parquet for "
            "backtest replay. Hackathon scope: export only — the "
            "full replay loop is post-deadline."
        )
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Postgres DSN. Falls back to DATABASE_URL env when omitted.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Output directory (default {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Only export cycles with started_at >= this UTC ISO timestamp.",
    )
    parser.add_argument(
        "--stem",
        default=None,
        help=(
            "Optional filename stem suffix (e.g. `20260529` → "
            "`cycles_20260529.parquet`). Default uses no suffix."
        ),
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    # `UTC` import kept for symmetry with other CLI modules even if
    # the local _parse_iso doesn't directly reference it.
    assert UTC is not None
    _main()
