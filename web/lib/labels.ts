// Human-readable labels for the technical enums the agent emits.
//
// Single source of truth for the UI — venue ids, wake reasons, cycle
// results, watcher event kinds, and executed-action kinds all surface as
// raw snake/colon strings in the API; this module maps them to branded,
// readable text. Venue ids mirror `VenueId` in
// `agent/agent/reason/venues.py`; event kinds mirror
// `agent/agent/sandbox/watcher.py`.
//
// All functions are pure and deterministic — safe for SSR (no locale or
// timezone dependence).

/** snake_case / colon-delimited → Title Case. Fallback for unknown kinds. */
function humanize(s: string): string {
  return s
    .replace(/[_:]+/g, " ")
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// Branded venue casing (LM, OnChain) — explicit, not derivable from humanize.
const VENUE_LABELS: Record<string, string> = {
  cash_usdc: "Cash USDC",
  aave_v3_usdc: "Aave V3 USDC",
  bybit_flex: "Bybit Flex",
  bybit_onchain: "Bybit OnChain",
  bybit_lm: "Bybit LM",
  bybit_dual_asset: "Bybit DualAsset",
  bybit_discount_buy: "Bybit DiscountBuy",
  bybit_smart_leverage: "Bybit SmartLeverage",
  bybit_double_win: "Bybit DoubleWin",
  bybit_alpha: "Bybit Alpha",
  bybit_hold_to_earn: "Bybit Hold-to-Earn",
  bybit_funding_carry: "Bybit FundingCarry",
  perp: "Bybit USDT-Perp",
};

export function venueLabel(id: string): string {
  const known = VENUE_LABELS[id];
  if (known) return known;
  // Synthetic cash positions: cash_usdt → "Cash USDT".
  const cash = id.match(/^cash_(.+)$/);
  if (cash) return `Cash ${cash[1].toUpperCase()}`;
  return humanize(id);
}

// Watcher event kinds (`agent/agent/sandbox/watcher.py`).
const EVENT_KIND_LABELS: Record<string, string> = {
  price_drift: "Price drift",
  funding_flip: "Funding flip",
  peg_drift: "Peg drift",
  da_settlement_window: "DualAsset settlement window",
  new_hold_to_earn: "New Hold-to-Earn",
  measured_yield_jump: "Measured-yield jump",
  lm_liquidation_distance: "LM liquidation distance",
  perp_liquidation_distance: "Perp liquidation distance",
  pick_invalidated: "Pick invalidated",
  earn_redeem_settled: "Earn redeem settled",
  carry_liq_close: "Carry liquidation close",
};

export function formatEventKind(kind: string): string {
  return EVENT_KIND_LABELS[kind] ?? humanize(kind);
}

/** "heartbeat" → "Heartbeat (4h)"; "event:price_drift" → "Price drift". */
export function formatWakeReason(reason: string): string {
  if (reason === "heartbeat") return "Heartbeat (4h)";
  if (reason.startsWith("event:")) return formatEventKind(reason.slice("event:".length));
  return humanize(reason);
}

const RESULT_LABELS: Record<string, string> = {
  ok: "OK",
  executed: "Executed",
  no_actions: "No actions",
  error: "Error",
  halted: "Halted",
};

/** "skipped:invalid" → "Skipped (invalid)"; "executed" → "Executed". */
export function formatResult(result: string): string {
  const known = RESULT_LABELS[result];
  if (known) return known;
  const skipped = result.match(/^skipped:(.+)$/);
  if (skipped) return `Skipped (${skipped[1]})`;
  return humanize(result);
}

/** Executed-action kind (e.g. "earn_subscribe" → "Earn Subscribe"). */
export function formatActionKind(kind: string): string {
  return humanize(kind);
}
