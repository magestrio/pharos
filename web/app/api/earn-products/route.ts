import { NextRequest, NextResponse } from "next/server";

import { readLatestSnapshot, type SnapshotJson, type BybitProduct } from "@/lib/snapshot";

export const dynamic = "force-dynamic";
export const revalidate = 0;

// All Bybit Earn products + current APR + Bybit's daily APR history +
// current funding rate. Primary source is the agent FastAPI (`/earn/products`,
// reads the latest snapshot from Postgres — live in prod and local `pnpm start`).
// Falls back to reading the latest snapshot file directly so a standalone
// `pnpm --filter web dev` (no API) still renders.

const BASE = (process.env.AGENT_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

const HOURS_PER_YEAR = 24 * 365;
const DEFAULT_FUNDING_INTERVAL_HOURS = 8;

export type EarnProductRow = {
  category: string;
  product_id: string;
  coin: string;
  effective_apr_pct: number;
  apr_source: string;
  apr_history_pct: number[] | null;
  funding_rate: number | null;
  funding_annual_pct: number | null;
  mark_price: number | null;
  // Coin-quality metrics. The FastAPI path computes these; the file-fallback
  // below leaves them null (no DB / cross-cycle data, scoring not duplicated).
  is_stable: boolean;
  avg_apr_7d_pct: number | null;
  net_apr_pct: number | null;
  apr_stability: number | null;
  price_volatility_pct: number | null;
  price_stability: number | null;
  stability_score: number | null;
  funding_7d_annual_pct: number | null;
  quality_score: number | null;
  profit_1d: ProfitHorizon | null;
  profit_7d: ProfitHorizon | null;
  profit_30d: ProfitHorizon | null;
};

type ProfitHorizon = {
  earn_pct: number | null;
  funding_pct: number | null;
  fee_pct: number | null;
  break_even_days: number | null;
  total_pct: number | null;
  basis: string;
  note: string | null;
};

export type EarnProductsResponse = {
  captured_at: string | null;
  products: EarnProductRow[];
};

export async function GET(req: NextRequest) {
  const search = req.nextUrl.search;
  const upstream = await tryUpstream(search);
  if (upstream) return upstream;

  const snap = await readLatestSnapshot();
  if (!snap) {
    return NextResponse.json({ error: "no snapshot available" }, { status: 404 });
  }
  const url = req.nextUrl;
  const category = url.searchParams.get("category");
  const coin = url.searchParams.get("coin")?.toUpperCase();
  const limitRaw = parseInt(url.searchParams.get("limit") ?? "200", 10);
  const limit = Math.min(Math.max(Number.isFinite(limitRaw) ? limitRaw : 200, 1), 500);

  const rows = buildRows(snap, category, coin);
  rows.sort((a, b) => b.effective_apr_pct - a.effective_apr_pct);
  const body: EarnProductsResponse = {
    captured_at: snap.captured_at ?? null,
    products: rows.slice(0, limit),
  };
  return NextResponse.json(body);
}

async function tryUpstream(search: string): Promise<Response | null> {
  try {
    const res = await fetch(`${BASE}/earn/products${search}`, {
      cache: "no-store",
      headers: { accept: "application/json" },
    });
    if (!res.ok) return null;
    const body = await res.text();
    return new NextResponse(body, {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  } catch {
    return null;
  }
}

function annualFundingPct(rate: number | null, intervalHours: number | null): number | null {
  if (rate === null) return null;
  const interval = intervalHours && intervalHours > 0 ? intervalHours : DEFAULT_FUNDING_INTERVAL_HOURS;
  return rate * (HOURS_PER_YEAR / interval) * 100;
}

function num(value: string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const n = parseFloat(value);
  return Number.isFinite(n) ? n : null;
}

function buildRows(
  snap: SnapshotJson,
  category: string | null,
  coin: string | undefined,
): EarnProductRow[] {
  const funding = snap.earn_funding ?? {};
  const buckets = category && snap.products[category]
    ? { [category]: snap.products[category] }
    : snap.products;
  const rows: EarnProductRow[] = [];
  for (const [cat, items] of Object.entries(buckets)) {
    for (const p of items as BybitProduct[]) {
      const pcoin = (p.coin ?? "").toUpperCase();
      if (coin && !pcoin.includes(coin)) continue;
      const legs = [pcoin, ...pcoin.split("/")];
      const fund = legs.map((l) => funding[l.trim()]).find((f) => f !== undefined);
      const rate = fund ? num(fund.funding_rate) : null;
      const interval = fund ? num(fund.funding_interval_hours) : null;
      const hist = Array.isArray(p.apr_history_points) && p.apr_history_points.length
        ? p.apr_history_points.map((x) => (num(x) ?? 0) * 100)
        : null;
      rows.push({
        category: cat,
        product_id: p.product_id,
        coin: p.coin,
        effective_apr_pct: (num(p.effective_apr) ?? 0) * 100,
        apr_source: p.apr_source,
        apr_history_pct: hist,
        funding_rate: rate,
        funding_annual_pct: annualFundingPct(rate, interval),
        mark_price: fund ? num(fund.mark_price) : null,
        // Quality metrics need the FastAPI/DB path; null in the dev fallback.
        is_stable: false,
        avg_apr_7d_pct: null,
        net_apr_pct: null,
        apr_stability: null,
        price_volatility_pct: null,
        price_stability: null,
        stability_score: null,
        funding_7d_annual_pct: null,
        quality_score: null,
        profit_1d: null,
        profit_7d: null,
        profit_30d: null,
      });
    }
  }
  return rows;
}
