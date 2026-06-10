/**
 * Shared UI types + the static validator risk-flag legend.
 *
 * All fabricated pre-mainnet numbers (mock VAULT stats, AI/Human race
 * series, placeholder addresses, ticker items) have been removed - the
 * UI now reads everything from live hooks (`use-vault-stats`,
 * `use-reputation`, `agent-store-context`, `/api/capital-history`, …)
 * and contract addresses from `@/lib/contracts`. What's left here is
 * structural: shared row types and the deterministic-validator threshold
 * legend (a description of rules, not performance data).
 */

export type Allocation = {
  key: string;
  label: string;
  sub: string;
  pct: number;
  apy: number;
  color: string;
  notional: number;
  expandable?: boolean;
};

export type Decision = {
  id: string;
  full: string;
  ts: string;
  // Split human-readable parts for the decision-log two-line column
  // ("Jun 8, 2026" over "14:30 UTC"). `ts` keeps the combined string.
  dateLabel: string;
  timeLabel: string;
  ago: string;
  summary: string;
  risk: "LOW" | "MED" | "HIGH";
  exec: string;
  confidence: number;
  profitable: boolean;
  conviction: "low" | "med" | "high";
  thesis: string;
  // First-person "diary" note generated after the cycle's real outcome is
  // known (agent/sandbox/reflect.py). Primary human-readable view; the
  // structured `thesis` is the quant-detail behind a toggle. Undefined for
  // older cycles or when reflection generation was skipped.
  reflection?: string;
  risks: string;
  allora: string;
  flags: string[];
  ipfs: string;
  tx: string;
  // Off-chain cycle key for rich-detail expand. Rows derived purely from
  // an on-chain event (no matched cycle) leave this undefined.
  cycleTs?: string;
};

export type ActiveHedge = {
  key: string;
  label: string;
  venueSpot: string;
  venuePerp: string;
  spotQty: string;
  spotUsd: number;
  hedgeQty: string;
  hedgeUsd: number;
  netDelta: number;
  fundingEarned24h: number;
  spotApr: number;
  fundingApr: number;
  blendedApr: number;
  openedAgo: string;
  closeTrigger: string;
};

export type RiskFlag = {
  key: string;
  label: string;
  thresh: string;
  tone: "warn" | "red";
};

// Static description of the deterministic validator's risk-flag
// taxonomy + thresholds. This is documentation of the rules the agent
// is bound by, not live/performance data.
export const RISK_FLAGS: RiskFlag[] = [
  { key: "usdc_peg_deviation", label: "USDC peg deviation", thresh: ">50bps warn · >100bps action", tone: "warn" },
  { key: "attestor_lag_warning", label: "Attestor lag warning", thresh: "> 30 min", tone: "warn" },
  { key: "attestor_lag_critical", label: "Attestor lag critical", thresh: "> 60 min · forced exit", tone: "red" },
  { key: "funding_rate_negative_triggered", label: "Funding flipped negative", thresh: "basis trade auto-close", tone: "warn" },
  { key: "aave_utilization_high", label: "Aave utilization high", thresh: "> 90%", tone: "warn" },
  { key: "perp_orderbook_thin", label: "Perp orderbook thin", thresh: "depth < 10× position", tone: "warn" },
];
