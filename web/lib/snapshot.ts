import { readFile, readdir, stat } from "node:fs/promises";
import { join } from "node:path";

// Dev-only file-system reader for the sandbox snapshot/decision JSONs produced
// by `agent/agent/sandbox/snapshot.py` and `agent/agent/sandbox/decide.py`.
// In production these should come from an IPFS gateway or HTTP endpoint
// exposed by the Python agent.

const REPO_ROOT = join(process.cwd(), "..");
const SNAPSHOTS_DIR = join(REPO_ROOT, "agent", "agent", "sandbox", "snapshots");
const DECISIONS_DIR = join(REPO_ROOT, "agent", "agent", "sandbox", "decisions");

export type BybitProduct = {
  category: string;
  product_id: string;
  coin: string;
  effective_apr: string;
  apr_source: string;
  base_apr_string: string | null;
  redeem_lockup_minutes: number | null;
  notes: string[];
  // Bybit's daily APR series (fractional strings, oldest→newest), present
  // only for FlexibleSaving + OnChain products.
  apr_history_points?: string[] | null;
};

export type EarnFundingEntry = {
  symbol: string;
  funding_rate: string | null;
  funding_interval_hours: string | null;
  mark_price: string | null;
  source?: string;
};

export type EarnPosition = {
  productId: string;
  coin: string;
  amount: string;
  category: string;
  totalPnl: string;
  claimableYield: string;
  availableAmount: string;
};

export type WalletAccount = {
  accountType: string;
  totalEquity: string;
  valuationCurrency: string;
  coinDetail?: Array<{ coin: string; equity: string }>;
  categories?: Array<{ category: string; equity: string; coinDetail: Array<{ coin: string; equity: string }> }>;
};

export type SnapshotJson = {
  schema_version: number;
  captured_at: string;
  wallet: {
    total_equity_usd: string;
    accounts: WalletAccount[];
  };
  earn_positions: EarnPosition[];
  lm_positions: unknown[];
  products: Record<string, BybitProduct[]>;
  market: {
    btc_price: string;
    btc_24h_change_pct: string;
    btc_funding_rate: string;
    eth_price: string;
    eth_24h_change_pct: string;
    eth_funding_rate: string;
  };
  perp_market: Record<
    string,
    {
      symbol: string;
      funding_rate_8h: string;
      mark_price: string;
      orderbook_depth_50bps_usd: string;
    }
  >;
  earn_funding?: Record<string, EarnFundingEntry>;
  usdc_peg: {
    price_usd: string;
    deviation_bps: string;
    source: string;
    fetched_at: string;
  };
  errors: string[];
};

export type DecisionVenuePick = {
  product_id: string;
  weight: number;
  notes: string[];
};

export type DecisionVenue = {
  venue_id: string;
  weight: number;
  picks: DecisionVenuePick[];
};

export type DecisionJson = {
  thesis: string;
  venues: DecisionVenue[];
  hedges: unknown[];
  confidence: number;
  risk_flags: string[];
  notes: string[];
};

async function latestFile(dir: string): Promise<string | null> {
  try {
    const entries = await readdir(dir);
    const jsons = entries.filter((e) => e.endsWith(".json"));
    if (jsons.length === 0) return null;
    const withTimes = await Promise.all(
      jsons.map(async (name) => ({ name, mtime: (await stat(join(dir, name))).mtimeMs })),
    );
    withTimes.sort((a, b) => b.mtime - a.mtime);
    return join(dir, withTimes[0].name);
  } catch {
    return null;
  }
}

export async function readLatestSnapshot(): Promise<SnapshotJson | null> {
  const path = await latestFile(SNAPSHOTS_DIR);
  if (!path) return null;
  const raw = await readFile(path, "utf-8");
  return JSON.parse(raw) as SnapshotJson;
}

export async function readLatestDecision(): Promise<DecisionJson | null> {
  const path = await latestFile(DECISIONS_DIR);
  if (!path) return null;
  const raw = await readFile(path, "utf-8");
  return JSON.parse(raw) as DecisionJson;
}
