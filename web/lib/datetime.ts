// Human-readable date/time formatting for the UI.
//
// All formatters render in UTC explicitly (`timeZone: "UTC"`, `hourCycle:
// "h23"`) so the output is deterministic - identical on the server and the
// client. This matters in App-Router server components: a locale/timezone-
// dependent string would differ between the SSR pass and hydration and throw
// a React hydration mismatch. UTC also matches the agent's on-chain log,
// which is UTC throughout.
//
// Target format: "Jun 8, 2026, 14:30 UTC" (24h, short month, no seconds).
// Relative times ("5m ago") live next to the call sites that need them -
// they're already human-readable and aren't this module's concern.

const FALLBACK = "-";

function toDate(input: Date | number | string): Date | null {
  const d = input instanceof Date ? input : new Date(input);
  return Number.isNaN(d.getTime()) ? null : d;
}

const DATE_TIME_FMT = new Intl.DateTimeFormat("en-US", {
  timeZone: "UTC",
  hourCycle: "h23",
  month: "short",
  day: "numeric",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

const DATE_FMT = new Intl.DateTimeFormat("en-US", {
  timeZone: "UTC",
  month: "short",
  day: "numeric",
  year: "numeric",
});

const TIME_FMT = new Intl.DateTimeFormat("en-US", {
  timeZone: "UTC",
  hourCycle: "h23",
  hour: "2-digit",
  minute: "2-digit",
});

/** "Jun 8, 2026, 14:30 UTC" */
export function formatDateTime(input: Date | number | string): string {
  const d = toDate(input);
  return d ? `${DATE_TIME_FMT.format(d)} UTC` : FALLBACK;
}

/** "Jun 8, 2026" */
export function formatDate(input: Date | number | string): string {
  const d = toDate(input);
  return d ? DATE_FMT.format(d) : FALLBACK;
}

/** "14:30 UTC" */
export function formatTime(input: Date | number | string): string {
  const d = toDate(input);
  return d ? `${TIME_FMT.format(d)} UTC` : FALLBACK;
}
