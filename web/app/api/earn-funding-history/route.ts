import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

// Cross-cycle funding-rate series for one coin. This needs the agent's
// Postgres (it scans many snapshots), so it can only come from the FastAPI —
// there is no filesystem equivalent. In a standalone `pnpm --filter web dev`
// without the API we return an empty series so the chart shows an honest
// empty state instead of erroring.

const BASE = (process.env.AGENT_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export async function GET(req: NextRequest) {
  const coin = req.nextUrl.searchParams.get("coin")?.toUpperCase() ?? "";
  if (!coin) {
    return NextResponse.json({ error: "coin query param required" }, { status: 400 });
  }
  try {
    const res = await fetch(`${BASE}/earn/funding-history${req.nextUrl.search}`, {
      cache: "no-store",
      headers: { accept: "application/json" },
    });
    if (res.ok) {
      return new NextResponse(await res.text(), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
  } catch {
    // fall through to empty-state
  }
  return NextResponse.json({ coin, points: [] });
}
