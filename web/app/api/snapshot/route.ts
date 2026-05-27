import { NextResponse } from "next/server";

import { readLatestSnapshot } from "@/lib/snapshot";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  const snap = await readLatestSnapshot();
  if (!snap) {
    return NextResponse.json({ error: "no snapshot available" }, { status: 404 });
  }
  // Slim payload: drop the heavy products[] catalog and per-coin dust holdings.
  return NextResponse.json({
    captured_at: snap.captured_at,
    schema_version: snap.schema_version,
    wallet: {
      total_equity_usd: snap.wallet.total_equity_usd,
      accounts: snap.wallet.accounts.map((a) => ({
        accountType: a.accountType,
        totalEquity: a.totalEquity,
        valuationCurrency: a.valuationCurrency,
      })),
    },
    market: snap.market,
    perp_market: snap.perp_market,
    usdc_peg: snap.usdc_peg,
    earn_positions_count: snap.earn_positions.length,
    product_counts: Object.fromEntries(
      Object.entries(snap.products).map(([cat, items]) => [cat, items.length]),
    ),
    errors: snap.errors,
  });
}
