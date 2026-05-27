import { NextRequest, NextResponse } from "next/server";

import { readLatestSnapshot } from "@/lib/snapshot";

export const dynamic = "force-dynamic";
export const revalidate = 0;

const DEFAULT_LIMIT = 20;
const MAX_LIMIT = 100;

export async function GET(req: NextRequest) {
  const snap = await readLatestSnapshot();
  if (!snap) {
    return NextResponse.json({ error: "no snapshot available" }, { status: 404 });
  }

  const url = req.nextUrl;
  const category = url.searchParams.get("category");
  const coin = url.searchParams.get("coin")?.toUpperCase();
  const limitRaw = parseInt(url.searchParams.get("limit") ?? `${DEFAULT_LIMIT}`, 10);
  const limit = Math.min(Math.max(Number.isFinite(limitRaw) ? limitRaw : DEFAULT_LIMIT, 1), MAX_LIMIT);

  const buckets = category && snap.products[category]
    ? { [category]: snap.products[category] }
    : snap.products;

  const result: Record<string, ReturnType<typeof shapeProduct>[]> = {};
  for (const [cat, items] of Object.entries(buckets)) {
    const filtered = coin ? items.filter((p) => p.coin.toUpperCase().includes(coin)) : items;
    const sorted = [...filtered].sort(
      (a, b) => parseFloat(b.effective_apr || "0") - parseFloat(a.effective_apr || "0"),
    );
    result[cat] = sorted.slice(0, limit).map(shapeProduct);
  }

  return NextResponse.json({
    captured_at: snap.captured_at,
    category: category ?? null,
    coin: coin ?? null,
    limit,
    products: result,
  });
}

function shapeProduct(p: {
  category: string;
  product_id: string;
  coin: string;
  effective_apr: string;
  apr_source: string;
  base_apr_string: string | null;
  redeem_lockup_minutes: number | null;
  notes: string[];
}) {
  return {
    category: p.category,
    product_id: p.product_id,
    coin: p.coin,
    effective_apr: parseFloat(p.effective_apr || "0"),
    apr_source: p.apr_source,
    base_apr_string: p.base_apr_string,
    redeem_lockup_minutes: p.redeem_lockup_minutes,
    notes: p.notes,
  };
}
