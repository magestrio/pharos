"use client";

import { useMemo } from "react";
import { useCycles, useCycleDetail } from "@/lib/agent-store-context";

// Allora exposes only the 5m and 8h windows for these price topics; we
// surface the 8h forecast (the directional bias the agent acts on). The
// 5m window is short-horizon noise and is intentionally ignored on the UI.
const WINDOW = "8h";

// Same significance threshold the agent prompt uses: a forecast within
// ±0.3% of spot is treated as directionally flat (no bull/bear read).
const SIGNIFICANT_DELTA_PCT = 0.3;

export type MarketBias = "bullish" | "bearish" | "neutral";

export type AlloraForecast = {
  token: string; // "BTC" / "ETH" / "SOL"
  inferenceUsd: number;
  spotUsd: number | null; // null when snapshot has no spot for the coin (SOL)
  deltaPct: number | null;
  direction: "up" | "down" | "flat";
  asOf: number; // unix sec — when the inference was produced
};

export interface AlloraForecastResult {
  forecasts: AlloraForecast[];
  marketBias: MarketBias;
  isLoading: boolean;
  isError: boolean;
  isLive: boolean;
}

function toNum(v: unknown): number | null {
  if (v === null || v === undefined) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function directionFor(deltaPct: number | null): "up" | "down" | "flat" {
  if (deltaPct === null || Math.abs(deltaPct) < SIGNIFICANT_DELTA_PCT) return "flat";
  return deltaPct > 0 ? "up" : "down";
}

type RawInference = {
  token?: unknown;
  window?: unknown;
  inference_usd?: unknown;
  timestamp?: unknown;
};

/**
 * Latest cycle's Allora 8h price forecasts, joined against the snapshot's
 * spot prices to a per-coin delta. Spot exists only for BTC/ETH in the
 * snapshot, so SOL renders price-only (deltaPct === null). `marketBias`
 * is read off the BTC forecast — the agent's primary directional signal.
 *
 * Reuses the `useCycles({ limit: 50 })` cache (already fetched by the
 * stats row) and reads the latest cycle's full snapshot on demand.
 */
export function useAlloraForecast(): AlloraForecastResult {
  const cycles = useCycles({ limit: 50 });
  const cycleTs = cycles.data?.[0]?.cycle_ts ?? null;
  const detail = useCycleDetail(cycleTs);

  return useMemo<AlloraForecastResult>(() => {
    const isLoading = cycles.isLoading || (!!cycleTs && detail.isLoading);
    const isError = cycles.isError || detail.isError;

    const market = (detail.data?.snapshot as Record<string, unknown> | null | undefined)
      ?.market as Record<string, unknown> | undefined;

    if (!market) {
      return { forecasts: [], marketBias: "neutral", isLoading, isError, isLive: false };
    }

    const spotByToken: Record<string, number | null> = {
      BTC: toNum(market.btc_price),
      ETH: toNum(market.eth_price),
    };

    const raw = Array.isArray(market.allora_inferences)
      ? (market.allora_inferences as RawInference[])
      : [];

    const forecasts: AlloraForecast[] = [];
    for (const inf of raw) {
      if (inf.window !== WINDOW) continue;
      const token = typeof inf.token === "string" ? inf.token : null;
      const inferenceUsd = toNum(inf.inference_usd);
      if (!token || inferenceUsd === null) continue;

      const spotUsd = spotByToken[token] ?? null;
      const deltaPct =
        spotUsd !== null && spotUsd > 0
          ? ((inferenceUsd - spotUsd) / spotUsd) * 100
          : null;
      forecasts.push({
        token,
        inferenceUsd,
        spotUsd,
        deltaPct,
        direction: directionFor(deltaPct),
        asOf: toNum(inf.timestamp) ?? 0,
      });
    }

    // Stable, predictable order: BTC, ETH, then the rest alphabetically.
    const ORDER: Record<string, number> = { BTC: 0, ETH: 1 };
    forecasts.sort(
      (a, b) =>
        (ORDER[a.token] ?? 9) - (ORDER[b.token] ?? 9) ||
        a.token.localeCompare(b.token),
    );

    const btc = forecasts.find((f) => f.token === "BTC");
    const marketBias: MarketBias =
      btc && btc.direction !== "flat"
        ? btc.direction === "up"
          ? "bullish"
          : "bearish"
        : "neutral";

    return {
      forecasts,
      marketBias,
      isLoading,
      isError,
      isLive: forecasts.length > 0,
    };
  }, [
    cycles.isLoading,
    cycles.isError,
    cycleTs,
    detail.data,
    detail.isLoading,
    detail.isError,
  ]);
}
