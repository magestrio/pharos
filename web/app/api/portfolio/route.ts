import { NextResponse } from "next/server";

import { readLatestSnapshot } from "@/lib/snapshot";

export const dynamic = "force-dynamic";
export const revalidate = 0;

const DUST_THRESHOLD_USD = 0.5;

export async function GET() {
  const snap = await readLatestSnapshot();
  if (!snap) {
    return NextResponse.json({ error: "no snapshot available" }, { status: 404 });
  }

  const activePositions = snap.earn_positions
    .filter((p) => parseFloat(p.amount || "0") > 0)
    .map((p) => ({
      productId: p.productId,
      coin: p.coin,
      category: p.category,
      amount: parseFloat(p.amount),
      totalPnl: parseFloat(p.totalPnl || "0"),
      claimableYield: parseFloat(p.claimableYield || "0"),
      availableAmount: parseFloat(p.availableAmount || "0"),
    }));

  const accounts = snap.wallet.accounts.map((a) => ({
    accountType: a.accountType,
    totalEquity: parseFloat(a.totalEquity || "0"),
    valuationCurrency: a.valuationCurrency,
    // Only surface coins above dust threshold to keep payload small.
    // Earn account uses `categories[]` instead of flat `coinDetail`.
    holdings: a.coinDetail
      ? a.coinDetail
          .map((c) => ({ coin: c.coin, equity: parseFloat(c.equity || "0") }))
          .filter((c) => c.equity * usdHint(c.coin, snap) >= DUST_THRESHOLD_USD)
      : (a.categories ?? []).flatMap((cat) =>
          cat.coinDetail.map((c) => ({
            coin: c.coin,
            equity: parseFloat(c.equity || "0"),
            category: cat.category,
          })),
        ),
  }));

  return NextResponse.json({
    captured_at: snap.captured_at,
    total_equity_usd: parseFloat(snap.wallet.total_equity_usd),
    accounts,
    active_earn_positions: activePositions,
  });
}

// Rough USD hint for the dust filter - only need stablecoins to be 1:1
// and the rest to be priced at zero (they'll fall under the dust threshold).
function usdHint(coin: string, snap: { market: { btc_price: string; eth_price: string } }): number {
  const c = coin.toUpperCase();
  if (c === "USDT" || c === "USDC" || c === "USD1" || c === "RLUSD") return 1;
  if (c === "BTC") return parseFloat(snap.market.btc_price || "0");
  if (c === "ETH") return parseFloat(snap.market.eth_price || "0");
  return 0;
}
