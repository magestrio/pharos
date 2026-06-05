/**
 * Capital-growth time series, reconstructed from per-cycle positions.
 *
 * Agent API stores cycle metadata in one table and per-cycle positions
 * in another; there is no equity_usd column on `cycles`. Until that
 * column exists, this route fetches the cycle list + every cycle's
 * positions and sums `amount_usd` per cycle to derive the equity
 * point. Runs server-side against the same FastAPI on Hetzner the
 * other `/api/store/*` routes proxy, so the N+1 fetches stay on the
 * upstream-side intranet and the browser sees one compact response.
 */
import { NextResponse } from "next/server";

const BASE = (process.env.AGENT_API_URL ?? "http://localhost:8000").replace(
  /\/$/,
  "",
);

type CycleSummary = {
  cycle_ts: string;
  result: string;
};

type Position = {
  amount_usd: string | null;
};

type CycleDetail = {
  cycle_ts: string;
  positions: Position[];
};

export type CapitalPoint = {
  ts: string;
  equityUsd: number;
};

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    cache: "no-store",
    headers: { accept: "application/json" },
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return (await res.json()) as T;
}

function sumPositions(positions: Position[]): number {
  let total = 0;
  for (const p of positions) {
    const v = parseFloat(p.amount_usd ?? "0");
    if (Number.isFinite(v)) total += v;
  }
  return total;
}

export async function GET(request: Request) {
  const url = new URL(request.url);
  const limit = Math.min(
    Math.max(parseInt(url.searchParams.get("limit") ?? "60", 10) || 60, 5),
    200,
  );

  try {
    const cycles = await getJson<CycleSummary[]>(`/cycles?limit=${limit}`);
    // Sort ascending by ts so the client can render left-to-right.
    const sorted = [...cycles].sort((a, b) =>
      a.cycle_ts.localeCompare(b.cycle_ts),
    );

    const points: CapitalPoint[] = [];
    // Cycle details fetched in parallel to keep latency bounded by the
    // slowest upstream call instead of N × per-cycle round-trip.
    const details = await Promise.all(
      sorted.map((c) =>
        getJson<CycleDetail>(`/cycles/${encodeURIComponent(c.cycle_ts)}`).catch(
          () => null,
        ),
      ),
    );

    for (let i = 0; i < sorted.length; i++) {
      const d = details[i];
      if (!d) continue;
      const equityUsd = sumPositions(d.positions ?? []);
      // Skip cycles with no positions recorded — they're noise (errors,
      // skipped:invalid cycles that never made it to the executor).
      if (equityUsd <= 0) continue;
      points.push({
        ts: sorted[i].cycle_ts,
        equityUsd: Math.round(equityUsd * 100) / 100,
      });
    }

    return NextResponse.json(
      {
        points,
        upstream_cycles: sorted.length,
        captured_at: new Date().toISOString(),
      },
      { status: 200 },
    );
  } catch (e) {
    const message = e instanceof Error ? e.message : "unreachable";
    return NextResponse.json(
      { detail: `upstream unreachable: ${message}` },
      { status: 502 },
    );
  }
}
