/**
 * Mock data for Vault8004 prototype.
 * All addresses/timestamps/numbers are mock pre-mainnet. Swap this file at deploy time.
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
  ago: string;
  summary: string;
  risk: "LOW" | "MED" | "HIGH";
  exec: string;
  confidence: number;
  profitable: boolean;
  conviction: "low" | "med" | "high";
  thesis: string;
  risks: string;
  allora: string;
  flags: string[];
  ipfs: string;
  tx: string;
  // Off-chain cycle key for rich-detail expand (.9). Mock rows leave
  // this undefined so the expand falls back to the canned thesis.
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

export const VAULT = {
  id: "VAULT8004",
  productName: "vUSDC",
  erc8004: "#001",
  status: "ACTIVE",
  reputation: 847,
  reputationMax: 1000,
  exchangeRate: 1.01284,
  exchangeRateStart: 1.0,
  totalSupply: 1_231_804.5,
  tvlUsdc: 1_247_830.0,
  tvlDelta: 47_000.0,
  cumReturn: 1.284,
  apyEffective: 22.3,
  sharpe: 1.82,
  maxDD: 0.0,
  winRate: 78,
  decisions: 14,
  model: "Claude Opus 4.7",
  chain: "Mantle Mainnet",
  inception: "2026-05-03",
  daysLive: 21,
};

export const HEDGE_LIFETIME_FUNDING = 147.3;

export const ATTESTOR = {
  safeAddress: "0x4dc4a70Ae02d7ca2F3A06b1231b3A9312d82a037",
  safeLabel: "0x4dc4…a037",
  multisig: "2-of-3 Gnosis Safe",
  lastPushAgo: "3 min",
  lastPushMin: 3,
  consecutivePushes: 287,
  pushFrequency: "~5 min cron",
  status: "HEALTHY",
  laggedPushesLast24h: 0,
  warningThreshold: 30,
  criticalThreshold: 60,
};

export const RISK_FLAGS: RiskFlag[] = [
  { key: "usdc_peg_deviation", label: "USDC peg deviation", thresh: ">50bps warn · >100bps action", tone: "warn" },
  { key: "attestor_lag_warning", label: "Attestor lag warning", thresh: "> 30 min", tone: "warn" },
  { key: "attestor_lag_critical", label: "Attestor lag critical", thresh: "> 60 min · forced exit", tone: "red" },
  { key: "funding_rate_negative_triggered", label: "Funding flipped negative", thresh: "basis trade auto-close", tone: "warn" },
  { key: "aave_utilization_high", label: "Aave utilization high", thresh: "> 90%", tone: "warn" },
  { key: "perp_orderbook_thin", label: "Perp orderbook thin", thresh: "depth < 10× position", tone: "warn" },
];

function genMonotonicRate(start: number, end: number, n: number, seed: number): number[] {
  const out = new Array(n);
  out[0] = start;
  out[n - 1] = end;
  const total = end - start;
  let s = seed;
  const rand = () => {
    s = (s * 1664525 + 1013904223) % 4294967296;
    return s / 4294967296;
  };
  const weights: number[] = [];
  for (let i = 0; i < n - 1; i++) weights.push(0.5 + rand());
  const sum = weights.reduce((a, b) => a + b, 0);
  let cum = start;
  for (let i = 1; i < n - 1; i++) {
    cum += (weights[i - 1] / sum) * total;
    out[i] = cum;
  }
  return out.map((v) => Math.round(v * 100000) / 100000);
}

export const EXCHANGE_RATE_SERIES = genMonotonicRate(1.0, 1.01284, 22, 99);

// Mock TVL baseline for the capital-growth chart. Real TVL comes from
// `useVaultStats().tvlUsdc` once mainnet vUSDC is deployed; until then
// the chart visualises a $1M reference principal compounding at the
// same exchange-rate growth curve (vUSDC has no MTM — capital = shares
// × exchange_rate by design, so the curve shape mirrors EXCHANGE_RATE_SERIES).
export const INITIAL_CAPITAL_USD = 1_000_000;
export const CAPITAL_SERIES = EXCHANGE_RATE_SERIES.map(
  (rate) => rate * INITIAL_CAPITAL_USD,
);

function genSeries(target: number, vol: number, seed: number): number[] {
  const n = 22;
  const out = new Array(n);
  out[0] = 1_000_000;
  let s = seed;
  const rand = () => {
    s = (s * 1664525 + 1013904223) % 4294967296;
    return s / 4294967296;
  };
  const raw: number[] = [];
  for (let i = 1; i < n; i++) raw.push((rand() - 0.5) * vol);
  let cum = 0;
  for (let i = 0; i < raw.length; i++) cum += raw[i];
  const scale = target / cum;
  let level = 1_000_000;
  for (let i = 1; i < n; i++) {
    level = level + level * ((raw[i - 1] * scale) / 100);
    out[i] = level;
  }
  out[n - 1] = 1_000_000 * (1 + target / 100);
  return out.map((v) => Math.round(v * 100) / 100);
}

export const HUMAN_SERIES = genSeries(0.71, 0.4, 7);
export const AI_SERIES = genSeries(1.284, 0.6, 13);

export const HUMAN_STATS = {
  return: 0.71,
  sharpe: 1.04,
  dd: -0.3,
  rebalances: 3,
  strategy: "60% Aave V3 USDC / 30% Aave V3 WETH / 10% cash. Weekly rebalance. No Bybit / CEX exposure.",
  rationale: "Retail-realistic baseline: on-chain only, no CEX accounts, no off-chain attestor trust.",
  final: HUMAN_SERIES[HUMAN_SERIES.length - 1],
  apyAnnualized: 12.3,
};

export const AI_STATS = {
  return: 1.284,
  sharpe: 1.82,
  dd: 0.0,
  decisions: 14,
  strategy:
    "Multi-venue allocation across Aave V3 + Bybit Earn (200+ products) with delta-neutral hedging on volatile positions. Event-driven rebalancing + 4h cron fallback.",
  rationale: "Full venue access including CEX-side Earn products, gated through 2-of-3 attestor multisig.",
  final: AI_SERIES[AI_SERIES.length - 1],
  apyAnnualized: 22.3,
};

export const TICKER_ITEMS = [
  { k: "vUSDC", v: "$1.01284", d: "exchange rate", up: true },
  { k: "Aave V3 USDC", v: "4.23%", d: "supply APY", up: true },
  { k: "Bybit USDC Flex", v: "5.12%", d: "earn APR", up: true },
  { k: "USDC Peg", v: "$0.9994", d: "−6 bps", up: false },
  { k: "Attestor Lag", v: "3 min", d: "healthy", up: true },
  { k: "Mantle Gas", v: "0.018 gwei", d: "−1.20%", up: false },
  { k: "ETH/USD", v: "$3,847.21", d: "+1.84%", up: true },
  { k: "BTC/USD", v: "$92,104", d: "+0.62%", up: true },
  { k: "Allora ETH 7d", v: "62% bull", d: "fresh", up: true },
  { k: "SOL Funding 8h", v: "+0.018%", d: "positive", up: true },
  { k: "ETH Funding 8h", v: "+0.022%", d: "positive", up: true },
  { k: "TVL", v: "$1.247M", d: "+3.91%", up: true },
];

export const CONTRACTS = [
  { label: "vUSDC TOKEN", hash: "0x000000000000000000000000000000000000C0DE", placeholder: true },
  { label: "CAPITAL MANAGER", hash: "0x000000000000000000000000000000000000CA91", placeholder: true },
  { label: "BYBIT ATTESTOR", hash: "0x4dc4a70Ae02d7ca2F3A06b1231b3A9312d82a037", placeholder: false },
  { label: "DECISION LOG", hash: "0x00000000000000000000000000000000000d6C00", placeholder: true },
  { label: "REPUTATION ORACLE", hash: "0x0000000000000000000000000000000000Re9bAd", placeholder: true },
  { label: "ERC-8004 REGISTRY", hash: "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63", placeholder: false },
  { label: "HUMAN PM BASELINE", hash: "0x00000000000000000000000000000000B45e1ABe", placeholder: true },
  { label: "GNOSIS SAFE OWNER", hash: "0x4dc4a70Ae02d7ca2F3A06b1231b3A9312d82a037", placeholder: false, sub: "2-of-3" },
];

export const CIAN_COMPARE = [
  { dim: "Strategy type", cian: "Fixed loop", vault: "AI-curated dynamic" },
  { dim: "Decision making", cian: "None (deterministic)", vault: "Claude Opus 4.7" },
  { dim: "On-chain decision log", cian: "No", vault: "Yes (DecisionLog)" },
  { dim: "Verifiable reputation", cian: "No", vault: "Yes (ERC-8004)" },
  { dim: "Venues", cian: "Aave + Ethena only", vault: "Aave + Bybit Earn (200+)" },
  { dim: "Hedging", cian: "Built into structure", vault: "Dynamic per-position" },
  { dim: "Underlying yield source", cian: "USDe loop only", vault: "Multi-source basis trades" },
  { dim: "Trust model", cian: "Curator (Cian team)", vault: "Attestor (Safe) + AI track record" },
];
