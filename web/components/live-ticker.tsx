"use client";

import { useMemo } from "react";

import { Ticker } from "@/components/ui";
import { useBybitEarn, usePortfolio, useSnapshot } from "@/lib/live";
import { useIsMounted } from "@/lib/hooks/use-is-mounted";

type TickerItem = { k: string; v: string; d: string; up: boolean };

// `fallback` is kept for API compatibility but no longer renders by
// default — showing mock ticker items behind real data is exactly the
// kind of dishonest UI we're stripping out.
export function LiveTicker({ fallback: _fallback }: { fallback?: TickerItem[] } = {}) {
  const mounted = useIsMounted();
  const snapshot = useSnapshot();
  const portfolio = usePortfolio();
  const flex = useBybitEarn({ category: "FlexibleSaving", limit: 3 });

  const items = useMemo<TickerItem[]>(() => {
    if (!mounted) return [];
    const out: TickerItem[] = [];

    if (portfolio.data) {
      out.push({
        k: "Vault Equity",
        v: `$${formatUsd(portfolio.data.total_equity_usd)}`,
        d: "live · sandbox",
        up: true,
      });
    }

    if (snapshot.data) {
      const m = snapshot.data.market;
      out.push({
        k: "BTC",
        v: `$${formatUsd(parseFloat(m.btc_price), 0)}`,
        d: `${formatPct(parseFloat(m.btc_24h_change_pct))} 24h`,
        up: parseFloat(m.btc_24h_change_pct) >= 0,
      });
      out.push({
        k: "ETH",
        v: `$${formatUsd(parseFloat(m.eth_price), 0)}`,
        d: `${formatPct(parseFloat(m.eth_24h_change_pct))} 24h`,
        up: parseFloat(m.eth_24h_change_pct) >= 0,
      });
      const peg = snapshot.data.usdc_peg;
      const dev = parseFloat(peg.deviation_bps);
      out.push({
        k: "USDC Peg",
        v: `$${parseFloat(peg.price_usd).toFixed(4)}`,
        d: `${dev >= 0 ? "+" : ""}${dev.toFixed(1)} bps`,
        up: Math.abs(dev) < 50,
      });
      const ethPerp = snapshot.data.perp_market.ETH;
      if (ethPerp) {
        const fr = parseFloat(ethPerp.funding_rate_8h) * 100;
        out.push({
          k: "ETH-PERP 8h",
          v: `${fr >= 0 ? "+" : ""}${fr.toFixed(4)}%`,
          d: fr >= 0 ? "long pays" : "short pays",
          up: fr >= 0,
        });
      }
    }

    const flexProducts = flex.data?.products.FlexibleSaving ?? [];
    const usdcFlex = flexProducts.find((p) => p.coin === "USDC");
    if (usdcFlex) {
      out.push({
        k: "Bybit USDC Flex",
        v: `${(usdcFlex.effective_apr * 100).toFixed(2)}%`,
        d: usdcFlex.apr_source,
        up: true,
      });
    }
    const top = flexProducts[0];
    if (top && top.coin !== "USDC") {
      out.push({
        k: `Bybit Top (${top.coin})`,
        v: `${(top.effective_apr * 100).toFixed(2)}%`,
        d: top.apr_source,
        up: true,
      });
    }

    if (snapshot.data) {
      out.push({
        k: "Snapshot",
        v: relativeTime(snapshot.data.captured_at),
        d: snapshot.data.errors.length === 0 ? "healthy" : `${snapshot.data.errors.length} errors`,
        up: snapshot.data.errors.length === 0,
      });
    }

    return out;
  }, [mounted, snapshot.data, portfolio.data, flex.data]);

  return <Ticker items={items} />;
}

function formatUsd(n: number, digits = 2): string {
  if (!Number.isFinite(n)) return "—";
  if (n >= 1000) return n.toLocaleString("en-US", { maximumFractionDigits: 0 });
  return n.toFixed(digits);
}

function formatPct(n: number): string {
  if (!Number.isFinite(n)) return "—";
  return `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
}

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return iso;
  const diffSec = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (diffSec < 60) return `${diffSec}s ago`;
  const m = Math.floor(diffSec / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}
