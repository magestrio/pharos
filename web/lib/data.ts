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

export const ALLOCATIONS: Allocation[] = [
  { key: "AAVE_USDC", label: "Aave V3 USDC", sub: "lending · on-chain", pct: 48, apy: 4.23, color: "#00FF88", notional: 599_158 },
  { key: "AAVE_WETH", label: "Aave V3 WETH", sub: "lending · on-chain", pct: 12, apy: 2.15, color: "#22E37A", notional: 149_740 },
  { key: "BYBIT", label: "Bybit Attestor", sub: "off-chain · multi-strategy", pct: 36, apy: 11.4, color: "#5B8FF9", notional: 449_220, expandable: true },
  { key: "CASH", label: "Cash USDC", sub: "idle · liquidity buffer", pct: 4, apy: 0.0, color: "#3F4860", notional: 49_913 },
];

export const BYBIT_SUB: Allocation[] = [
  { key: "FLEXIBLE_USDC", label: "USDC Flexible Earn", sub: "Bybit · 200+ products", pct: 18, apy: 5.12, color: "#5B8FF9", notional: 224_610 },
  { key: "SOL_BASIS", label: "SOL Basis Trade", sub: "Earn + perp hedge", pct: 8, apy: 12.0, color: "#7AA5FB", notional: 99_826 },
  { key: "ETH_BASIS", label: "ETH Basis Trade", sub: "Earn + perp hedge", pct: 6, apy: 9.9, color: "#A6BEFC", notional: 74_869 },
  { key: "BUFFER", label: "Bybit Cash Buffer", sub: "redeem latency cover", pct: 4, apy: 0.0, color: "#345FC2", notional: 49_913 },
];

export const ACTIVE_HEDGES: ActiveHedge[] = [
  {
    key: "SOL",
    label: "SOL basis trade",
    venueSpot: "Bybit Earn (SOL Flexible)",
    venuePerp: "Bybit USDT-Perp (SOL-PERP)",
    spotQty: "200 SOL",
    spotUsd: 30_000,
    hedgeQty: "SOL-PERP short, 1x",
    hedgeUsd: 30_000,
    netDelta: 0,
    fundingEarned24h: 8.4,
    spotApr: 5.4,
    fundingApr: 6.6,
    blendedApr: 12.0,
    openedAgo: "day-1",
    closeTrigger: "funding < -0.02% / 8h (8h-avg)",
  },
  {
    key: "ETH",
    label: "ETH basis trade",
    venueSpot: "Bybit Earn (ETH On-Chain)",
    venuePerp: "Bybit USDT-Perp (ETH-PERP)",
    spotQty: "5 ETH",
    spotUsd: 16_000,
    hedgeQty: "ETH-PERP short, 1x",
    hedgeUsd: 16_000,
    netDelta: 0,
    fundingEarned24h: 3.2,
    spotApr: 4.1,
    fundingApr: 5.8,
    blendedApr: 9.9,
    openedAgo: "day-1",
    closeTrigger: "funding < -0.02% / 8h (8h-avg)",
  },
];

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

export const DECISIONS: Decision[] = [
  { id: "0xa3f1...b2c4", full: "0xa3f1b2c47e9aD8c5F2A0B9D17c45Ee9Ab2c4b2c4", ts: "2026-05-23 22:00:12 UTC", ago: "2h ago", summary: "Rotated 15% from Aave USDC → Bybit Flexible USDC", risk: "LOW", exec: "4.8s", confidence: 0.81, profitable: true, conviction: "high", thesis: "Bybit Flexible USDC APR rose to 5.12% vs Aave V3 USDC at 4.23%. Spread +89bps justifies switch cost (gas + 12min latency). Attestor healthy (8min lag).", risks: "None.", allora: "Not used (yield arbitrage, not directional).", flags: [], ipfs: "QmRotateAaveBybit15", tx: "0xa3f1b2c4..." },
  { id: "0x7b29...f014", full: "0x7b294e8aD3F2bC5...", ts: "2026-05-23 18:00:08 UTC", ago: "6h ago", summary: "Opened SOL basis trade (delta-neutral)", risk: "MED", exec: "7.4s", confidence: 0.74, profitable: true, conviction: "high", thesis: "SOL Flexible Earn 5.4% APR + SOL-PERP funding +0.018%/8h = 16% blended APR. SOL perp depth $40M, safe for our $300k position. Hedged 1:1 with isolated 3x margin short.", risks: "Funding rate can flip negative — close trigger set at -0.02%/8h (8h-avg).", allora: "SOL funding forecast positive 24h horizon (67% confidence).", flags: [], ipfs: "QmOpenSolBasis", tx: "0x7b294e8a..." },
  { id: "0xc814...09ad", full: "0xc81409aD3c5e2bF7...", ts: "2026-05-23 14:00:00 UTC", ago: "10h ago", summary: "Reduced Bybit exposure 40% → 35%", risk: "MED", exec: "3.6s", confidence: 0.66, profitable: false, conviction: "med", thesis: "Attestor lag spiked to 45min during balance update window. Precautionary de-risk while team investigates. Bybit-side capital still accruing yield.", risks: "attestor_lag_warning triggered (lag > 30min).", allora: "Not used.", flags: ["attestor_lag_warning"], ipfs: "QmAttestorDerisk", tx: "0xc81409ad..." },
  { id: "0x4d72...b8e1", full: "0x4d72b8e1a3c5...", ts: "2026-05-23 10:00:09 UTC", ago: "14h ago", summary: "Closed ETH basis trade — funding flipped negative", risk: "LOW", exec: "5.2s", confidence: 0.83, profitable: true, conviction: "high", thesis: "ETH-PERP funding flipped to -0.024%/8h (24h avg). Basis trade now costs more than spot Earn yields. Exit before further degradation. Capital rotates back to USDC Flexible.", risks: "funding_rate_negative_triggered (policy auto-close).", allora: "ETH funding forecast bearish 24h (71% confidence).", flags: ["funding_rate_negative_triggered"], ipfs: "QmCloseEthBasis", tx: "0x4d72b8e1..." },
  { id: "0x9e05...44a2", full: "0x9e0544a2bF7c3eD1...", ts: "2026-05-23 06:00:03 UTC", ago: "18h ago", summary: "Held — USDC peg deviation within tolerance", risk: "LOW", exec: "0.9s", confidence: 0.88, profitable: true, conviction: "low", thesis: "USDC traded at $0.9994 for 20 minutes during Asian session. Deviation 6bps, well within 100bps threshold. No action needed.", risks: "usdc_peg_monitoring (informational).", allora: "Not used.", flags: ["usdc_peg_monitoring"], ipfs: "QmPegMonitor", tx: "0x9e0544a2..." },
  { id: "0x2c19...d801", full: "0x2c19d801aE4b...", ts: "2026-05-23 02:00:00 UTC", ago: "22h ago", summary: "Increased Aave V3 USDC supply +10%", risk: "LOW", exec: "3.1s", confidence: 0.77, profitable: true, conviction: "med", thesis: "Aave V3 USDC utilization dropped to 71% (from 84%), supply APR rose. Adding to Aave position to capture rate increase. Bybit position trimmed proportionally — current Bybit/Aave APR spread narrowed.", risks: "None. Aave utilization in healthy range.", allora: "Not used.", flags: [], ipfs: "QmAaveIncrease", tx: "0x2c19d801..." },
  { id: "0xb6f4...1a39", full: "0xb6f41a39C8e2bD7...", ts: "2026-05-22 22:00:05 UTC", ago: "1d 2h ago", summary: "Opened ETH basis trade (delta-neutral)", risk: "MED", exec: "6.9s", confidence: 0.71, profitable: true, conviction: "high", thesis: "ETH On-Chain Earn 4.1% + ETH-PERP funding +0.022%/8h = 14% blended. Less than SOL basis trade but higher liquidity / lower risk on hedge unwind.", risks: "None.", allora: "ETH funding 24h forecast positive (62% confidence).", flags: [], ipfs: "QmOpenEthBasis", tx: "0xb6f41a39..." },
  { id: "0xe042...77c5", full: "0xe04277c5D1a3bF8...", ts: "2026-05-22 14:00:11 UTC", ago: "1d 10h ago", summary: "Trimmed SOL basis trade — perp depth thinning", risk: "MED", exec: "4.0s", confidence: 0.64, profitable: true, conviction: "med", thesis: "SOL-PERP 1% orderbook depth dropped from $40M to $14M over 6 hours. Position size now ~2% of book — above 1% policy tolerance. Trimmed exposure 30% to restore safety margin.", risks: "perp_orderbook_thin (depth < 10× position).", allora: "Not used (liquidity-driven).", flags: ["perp_orderbook_thin"], ipfs: "QmTrimSol", tx: "0xe04277c5..." },
  { id: "0x33d8...80be", full: "0x33d880beA7c2F4...", ts: "2026-05-22 06:00:00 UTC", ago: "1d 18h ago", summary: "Held — Bybit / Aave spread under 30bps", risk: "LOW", exec: "0.7s", confidence: 0.62, profitable: true, conviction: "low", thesis: "Spread between Bybit Flexible USDC (4.41%) and Aave V3 USDC (4.18%) tightened to 23bps. Below 50bps action threshold. Holding to avoid round-trip cost.", risks: "None.", allora: "Not used.", flags: [], ipfs: "QmHoldSpread", tx: "0x33d880be..." },
  { id: "0x5710...2f8a", full: "0x57102f8aE3c4bD1...", ts: "2026-05-21 18:00:08 UTC", ago: "2d 6h ago", summary: "Aave WETH supply opened (12% of TVL)", risk: "LOW", exec: "5.7s", confidence: 0.69, profitable: true, conviction: "med", thesis: "WETH borrow demand on Aave V3 lifted supply APR to 2.15%. Smaller leg, but uncorrelated with USDC strategies — diversifies on-chain yield source. Position sized to avoid concentration breach.", risks: "WETH price exposure on this leg (intentional, capped at 15%).", allora: "ETH/USD 7d forecast neutral (53%).", flags: [], ipfs: "QmAaveWeth", tx: "0x57102f8a..." },
  { id: "0xaa90...c5e7", full: "0xaa90c5e7D2bF1aC4...", ts: "2026-05-21 06:00:00 UTC", ago: "2d 18h ago", summary: "Held — attestor lag normalized after Bybit maintenance", risk: "LOW", exec: "0.8s", confidence: 0.79, profitable: true, conviction: "med", thesis: "Attestor lag returned to 4-7min range after scheduled Bybit balance API maintenance. No action — system healthy.", risks: "None. Attestor monitoring resumed normal cadence.", allora: "Not used.", flags: [], ipfs: "QmAttestorRecovered", tx: "0xaa90c5e7..." },
  { id: "0x6c1b...39ef", full: "0x6c1b39efA8c2dD4...", ts: "2026-05-20 14:00:07 UTC", ago: "3d 10h ago", summary: "Scaled into Bybit USDC Flexible (10% → 20%)", risk: "LOW", exec: "4.4s", confidence: 0.8, profitable: true, conviction: "high", thesis: "Bybit USDC Flexible APR stable at 5.0%+ for 7 days. Attestor reliability proven (96h continuous push streak). Doubling position size — sub-allocation policy allows up to 25%.", risks: "None. Within concentration policy.", allora: "Not used.", flags: [], ipfs: "QmScaleBybit", tx: "0x6c1b39ef..." },
  { id: "0x1f44...80a1", full: "0x1f4480a1bE3c2D7...", ts: "2026-05-15 10:00:02 UTC", ago: "8d 14h ago", summary: "Held — perp funding rates neutral across venues", risk: "LOW", exec: "0.6s", confidence: 0.55, profitable: true, conviction: "low", thesis: "SOL/ETH/BTC perp funding all within ±0.005%/8h band. Basis trade thesis requires sustained positive funding. Waiting for clearer signal.", risks: "Opportunity cost only.", allora: "Funding topic confidence below 60% action floor.", flags: [], ipfs: "QmHoldFunding", tx: "0x1f4480a1..." },
  { id: "0xfe2a...0001", full: "0xfe2a000118AbC7D3...", ts: "2026-05-03 00:00:00 UTC", ago: "21d ago", summary: "Initial allocation deployed — inception", risk: "LOW", exec: "14.2s", confidence: 0.91, profitable: true, conviction: "high", thesis: "Inception allocation post-pivot. 70% Aave V3 USDC / 25% Bybit Flexible USDC / 5% cash. Conservative bootstrap — Bybit-side starts safe, scales to active strategies as attestor reliability proven.", risks: "Inception risk: first executeAllocation tx.", allora: "Not used.", flags: [], ipfs: "QmGenesisAlloc", tx: "0xfe2a0001..." },
];

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
