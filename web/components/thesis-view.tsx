"use client";

/**
 * Structured render of an agent decision thesis.
 *
 * The Claude prompt produces a single rationale blob. We parse it into
 * the canonical sections a quant reader expects — TL;DR, market
 * SIGNALS as chips, REGIME (1-2 lines), REASONING, optional SELF-CORRECTION
 * inside a `[show working ▾]` toggle, and the final ALLOCATION shown as
 * a stacked-bar with the blended-APR formula spelled out.
 *
 * The blob is unstructured, so the parser is a best-effort heuristic:
 *   • signals = `<label> = <number><unit>` / `funding_rate_*` matches
 *   • self-correction = sentences led by `Wait`, `Actually`, `Hmm`,
 *     `Let me re-check`, `Re-check`, `Hold on`, `On second thought`
 *   • allocation = explicit `Allocation:` header OR a closing sentence
 *     listing `<venue> <pct>%` triples
 *   • regime = leading 1-2 sentences before any of the above kicks in
 *   • reasoning = whatever's left after removing the categories above
 *
 * Anything that doesn't parse falls back to a clean mono pre-wrap. We
 * deliberately avoid italic serif on the body — the editorial pull-quote
 * read clearly hurts readability of dense numeric prose. Serif italic
 * is reserved for short headlines / eyebrows.
 */

import React, { useState } from "react";

// ─────────────────────────── parsing ─────────────────────────────────

interface AnalysisItem {
  index: number;
  body: string;
  verdict: "KEEP" | "REJECT" | null;
}

interface SignalChip {
  label: string;
  value: string;
  tone: "neutral" | "pos" | "warn" | "danger" | "elec";
}

interface AllocationLeg {
  label: string;
  pct: number;
  color: string;
}

interface ParsedThesis {
  /** Original blob, kept for the raw-view fallback. */
  raw: string;
  /** Either format: "Key analysis: …" produces structured analysis items. */
  analysis: AnalysisItem[];
  /** Leading 1-2 sentences describing the market regime. */
  regime: string | null;
  /** Free-form chips harvested from the body. */
  signals: SignalChip[];
  /** Main reasoning paragraph(s), self-correction stripped out. */
  reasoning: string | null;
  /** Lines the model used to second-guess itself. */
  selfCorrection: string | null;
  /** The closing `Allocation: …` or equivalent. */
  allocation: string | null;
  /** Parsed allocation legs (best-effort) for the stacked bar. */
  allocationLegs: AllocationLeg[];
  /** Blended-APR closing formula, if we found one. */
  blendedFormula: string | null;
}

const HEADER_KEY_ANALYSIS = /^key analysis:?$/i;
const HEADER_ALLOCATION = /^allocation:?\s*(.*)$/i;
const NUMBERED = /^(\d+)\.\s+(.*)$/;

const SELF_CORRECTION_LEADS = [
  "Wait",
  "Wait,",
  "Wait:",
  "Actually",
  "Actually,",
  "Hmm",
  "Hmm,",
  "Hold on",
  "Let me re-check",
  "Let me reconsider",
  "Let me reconsider:",
  "Re-check",
  "On second thought",
  "Correction",
  "Correction:",
];

function looksLikeSelfCorrection(sentence: string): boolean {
  const head = sentence.trimStart();
  return SELF_CORRECTION_LEADS.some((lead) => head.startsWith(lead));
}

function splitSentences(text: string): string[] {
  // Conservative sentence splitter — won't break on decimals or
  // shorthand like `$1.99/TON`. Splits on `. `, `! `, `? ` followed
  // by an uppercase letter.
  return text
    .split(/(?<=[.!?])\s+(?=[A-Z(\[$])/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function detectVerdict(s: string): "KEEP" | "REJECT" | null {
  const tail = s.trim().toUpperCase().replace(/\.$/, "").trim();
  if (tail.endsWith("REJECT") || /\bREJECT\b\s*$/.test(tail)) return "REJECT";
  if (tail.endsWith("KEEP") || /\bKEEP\b\s*$/.test(tail)) return "KEEP";
  return null;
}

// ─────────────────────────── signal extraction ───────────────────────

// Matches `label = value` and `label: value` patterns where the value
// is a recognisable metric (number + %, $, bps, /coin, multiplier).
const SIGNAL_PATTERNS: Array<{
  re: RegExp;
  build: (m: RegExpMatchArray) => SignalChip | null;
}> = [
  {
    // USDC peg deviation: "USDC $0.9996 (−3.74 bps)" or "peg −3.74 bps"
    re: /USDC\s+(?:peg\s+)?[-+]?\$?\d+\.\d+\s*\(?\s*([+-]?\d+\.?\d*)\s*bps\)?/i,
    build: (m) => ({
      label: "USDC peg",
      value: `${parseFloat(m[1]) >= 0 ? "+" : ""}${m[1]} bps`,
      tone: Math.abs(parseFloat(m[1])) < 10 ? "neutral" : "warn",
    }),
  },
  {
    // 24h change: "BTC -2.86%" / "ETH +1.2%"
    re: /\b(BTC|ETH|SOL|TON|ATOM)\b[^.]{0,40}?([+-]\d+\.\d+%)\s*(?:24h|24-?hour)?/gi,
    build: (m) => {
      const n = parseFloat(m[2]);
      return {
        label: `${m[1]} 24h`,
        value: m[2],
        tone: n >= 0 ? "pos" : "danger",
      };
    },
  },
  {
    // Funding rate snippets
    re: /funding_rate_[a-z0-9_]+\s*=\s*([+-]?\d+\.\d+(?:e[+-]?\d+)?)/gi,
    build: (m) => {
      const n = parseFloat(m[1]);
      return {
        label: "Funding 7d",
        value: m[1],
        tone: n >= 0 ? "pos" : "warn",
      };
    },
  },
  {
    // Effective yield / APR per product: "TON OnChain effective_yield = 18.52%"
    re: /\b([A-Z0-9]{2,6})\s+(OnChain|Flex|LM|HoldToEarn|DiscountBuy|DualAsset|Alpha|Perp)\b[^.]{0,60}?(?:effective_?yield|APR|apr)\s*[=:]\s*(\d+\.?\d*%)/gi,
    build: (m) => ({
      label: `${m[1]} ${m[2]} APR`,
      value: m[3],
      tone: "pos",
    }),
  },
  {
    // Orderbook depth: "depth = $476k" / "depth $424k"
    re: /\bdepth\s*[=:]?\s*\$([0-9.]+)\s*([kmKM])?/g,
    build: (m) => ({
      label: "Depth",
      value: `$${m[1]}${m[2] ?? ""}`,
      tone: "neutral",
    }),
  },
];

function extractSignals(text: string): SignalChip[] {
  const chips: SignalChip[] = [];
  const seen = new Set<string>();
  for (const { re, build } of SIGNAL_PATTERNS) {
    if (re.global) {
      let m: RegExpExecArray | null;
      const r = new RegExp(re.source, re.flags);
      while ((m = r.exec(text)) !== null) {
        const chip = build(m);
        if (!chip) continue;
        const k = `${chip.label}=${chip.value}`;
        if (seen.has(k)) continue;
        seen.add(k);
        chips.push(chip);
      }
    } else {
      const m = text.match(re);
      if (m) {
        const chip = build(m);
        if (chip) {
          const k = `${chip.label}=${chip.value}`;
          if (!seen.has(k)) {
            seen.add(k);
            chips.push(chip);
          }
        }
      }
    }
  }
  return chips.slice(0, 8); // cap so it stays a single row
}

// ─────────────────────────── allocation extraction ───────────────────

// "cash 15%, USD1 Flex 70%, TON OnChain 15%" → [{cash:15},{USD1 Flex:70},...]
const ALLOC_LEG_RX = /([A-Za-z0-9 _.-]+?)\s+(\d+(?:\.\d+)?)\s*%/g;

const ALLOC_PALETTE: Record<string, string> = {
  cash: "#3F4860",
  "usd1 flex": "#F5B400",
  "usd1 flexible": "#F5B400",
  "ton onchain": "#D9A005",
  "atom onchain": "#C99500",
  flex: "#F5B400",
  onchain: "#D9A005",
  perp: "#5B8FF9",
};

function legColor(label: string, idx: number): string {
  const lower = label.toLowerCase().trim();
  for (const k of Object.keys(ALLOC_PALETTE)) {
    if (lower.includes(k)) return ALLOC_PALETTE[k];
  }
  const fallback = ["#F5B400", "#FFC533", "#D9A005", "#5B8FF9", "#3F4860"];
  return fallback[idx % fallback.length];
}

function parseAllocationLegs(text: string | null): AllocationLeg[] {
  if (!text) return [];
  const legs: AllocationLeg[] = [];
  let m: RegExpExecArray | null;
  const r = new RegExp(ALLOC_LEG_RX.source, ALLOC_LEG_RX.flags);
  while ((m = r.exec(text)) !== null) {
    const pct = parseFloat(m[2]);
    if (!Number.isFinite(pct) || pct <= 0) continue;
    legs.push({
      label: m[1].trim(),
      pct,
      color: legColor(m[1], legs.length),
    });
  }
  // Drop legs that summed to >120% — likely false-positives from prose
  // where multiple "%" tokens get mis-paired with random words.
  const sum = legs.reduce((s, l) => s + l.pct, 0);
  if (sum > 120) return [];
  return legs;
}

// Pull a blended-APR or weighted-sum line: "0.70×3.26% + 0.15×20.3% = 5.33%"
const BLENDED_RX = /(\d+(?:\.\d+)?\s*[×x*]\s*\d+(?:\.\d+)?%[^.\n]*=\s*\d+(?:\.\d+)?%)/;

function extractBlendedFormula(text: string): string | null {
  const m = text.match(BLENDED_RX);
  return m ? m[1].replace(/x/i, "×").trim() : null;
}

// ─────────────────────────── master parser ───────────────────────────

function parseThesis(raw: string): ParsedThesis {
  const lines = raw.split(/\r?\n/);

  // First pass: pull out structured `Key analysis:` block and any
  // explicit `Allocation:` header. Whatever isn't claimed by those
  // ends up in `bodyBuf` which we then split into regime / reasoning
  // / self-correction by sentence-level inspection.
  let mode: "body" | "analysis" | "allocation" = "body";
  const bodyBuf: string[] = [];
  const analysis: AnalysisItem[] = [];
  let allocationRaw: string | null = null;
  let current: AnalysisItem | null = null;

  const flushItem = () => {
    if (current) {
      current.body = current.body.trim();
      current.verdict = detectVerdict(current.body);
      analysis.push(current);
      current = null;
    }
  };

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (!line.trim()) {
      if (mode === "analysis") flushItem();
      continue;
    }
    if (HEADER_KEY_ANALYSIS.test(line.trim())) {
      flushItem();
      mode = "analysis";
      continue;
    }
    const allocMatch = line.match(HEADER_ALLOCATION);
    if (allocMatch) {
      flushItem();
      mode = "allocation";
      allocationRaw = allocMatch[1].trim() || null;
      continue;
    }
    if (mode === "analysis") {
      const numbered = line.trimStart().match(NUMBERED);
      if (numbered) {
        flushItem();
        current = {
          index: parseInt(numbered[1], 10),
          body: numbered[2],
          verdict: null,
        };
      } else if (current) {
        current.body += " " + line.trim();
      } else {
        bodyBuf.push(line);
      }
    } else if (mode === "allocation") {
      if (allocationRaw) allocationRaw += " " + line.trim();
      else allocationRaw = line.trim();
    } else {
      bodyBuf.push(line);
    }
  }
  flushItem();

  const body = bodyBuf.join("\n").trim();
  const signals = extractSignals(body || raw);

  // Sentence-level split of the body to separate regime / reasoning /
  // self-correction.
  const sentences = splitSentences(body.replace(/\n+/g, " "));
  const regimeSentences: string[] = [];
  const reasoningSentences: string[] = [];
  const selfCorrectionSentences: string[] = [];

  // First 1-2 sentences become regime as long as they look like a
  // market description (no self-correction marker).
  let regimeBudget = 2;
  for (const s of sentences) {
    if (looksLikeSelfCorrection(s)) {
      selfCorrectionSentences.push(s);
      continue;
    }
    if (regimeBudget > 0) {
      regimeSentences.push(s);
      regimeBudget--;
      continue;
    }
    reasoningSentences.push(s);
  }

  // Fallback: if our body never produced a regime line but we have an
  // `analysis` block, leave regime empty rather than shoving the first
  // analysis sentence in.
  const regime = regimeSentences.join(" ").trim() || null;
  const reasoning = reasoningSentences.join(" ").trim() || null;
  const selfCorrection =
    selfCorrectionSentences.join(" ").trim() || null;

  // Allocation: prefer the explicit `Allocation:` header; otherwise
  // try to detect a closing sentence with leg percentages.
  let allocation = allocationRaw;
  if (!allocation && reasoning) {
    const closingMatch = reasoning.match(/[^.]*\d+\s*%[^.]*\d+\s*%[^.]*\./);
    if (closingMatch) allocation = closingMatch[0].trim();
  }
  const allocationLegs = parseAllocationLegs(allocation);
  const blendedFormula = extractBlendedFormula(allocation ?? body);

  return {
    raw,
    analysis,
    regime,
    signals,
    reasoning,
    selfCorrection,
    allocation,
    allocationLegs,
    blendedFormula,
  };
}

// ─────────────────────────── inline tokens ───────────────────────────

const TOKEN_RX = new RegExp(
  [
    String.raw`(\bREJECT\b)`,
    String.raw`(\bKEEP\b)`,
    String.raw`([+-]?\d+(?:\.\d+)?%)`,
    String.raw`(\$\d+(?:,\d{3})*(?:\.\d+)?)`,
    String.raw`(\bfunding_rate_[a-z0-9_]+\b)`,
    String.raw`(\b(?:OnChain|Flex|FlexibleSaving|LM|DiscountBuy|DualAsset|HoldToEarn|Perp|Alpha)\b)`,
    String.raw`(\b(?:BTC|ETH|USDC|USDT|USDE|USD1|TON|ATOM|MON|JTO|XLM|NEAR|ID|IO|SOL|APT|DOT|ADA)\b)`,
  ].join("|"),
  "g",
);

function HighlightedInline({ text }: { text: string }) {
  const parts: React.ReactNode[] = [];
  let lastIdx = 0;
  let match: RegExpExecArray | null;
  TOKEN_RX.lastIndex = 0;
  while ((match = TOKEN_RX.exec(text)) !== null) {
    if (match.index > lastIdx) {
      parts.push(text.slice(lastIdx, match.index));
    }
    const tok = match[0];
    parts.push(tokenSpan(tok, match.index));
    lastIdx = match.index + tok.length;
  }
  if (lastIdx < text.length) parts.push(text.slice(lastIdx));
  return <>{parts}</>;
}

function tokenSpan(tok: string, key: number): React.ReactNode {
  if (tok === "REJECT") {
    return (
      <span key={key} className="text-danger font-semibold">
        {tok}
      </span>
    );
  }
  if (tok === "KEEP") {
    return (
      <span key={key} className="text-pos font-semibold">
        {tok}
      </span>
    );
  }
  if (/^[+-]?\d/.test(tok) && tok.endsWith("%")) {
    const num = parseFloat(tok);
    const color =
      Number.isFinite(num) && num < 0
        ? "text-danger"
        : Number.isFinite(num) && num >= 10
          ? "text-accent"
          : "text-white";
    return (
      <span key={key} className={`font-mono tabular ${color}`}>
        {tok}
      </span>
    );
  }
  if (tok.startsWith("$")) {
    return (
      <span key={key} className="font-mono tabular text-white">
        {tok}
      </span>
    );
  }
  if (/^funding_rate_/.test(tok)) {
    return (
      <span key={key} className="font-mono text-elec/90 text-[0.95em]">
        {tok}
      </span>
    );
  }
  return (
    <span key={key} className="text-white/90">
      {tok}
    </span>
  );
}

// ─────────────────────────── presentational bits ─────────────────────

function ThesisEyebrow({ children }: { children: React.ReactNode }) {
  return (
    <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-accent mb-2.5">
      {children}
    </div>
  );
}

function SignalRow({ chips }: { chips: SignalChip[] }) {
  const toneCls: Record<SignalChip["tone"], string> = {
    neutral: "bg-ink-800 text-white border-ink-600/70",
    pos: "bg-pos/[0.08] text-pos border-pos/30",
    warn: "bg-warn/[0.08] text-warn border-warn/30",
    danger: "bg-danger/[0.08] text-danger border-danger/30",
    elec: "bg-elec/[0.08] text-elec border-elec/30",
  };
  return (
    <div className="flex flex-wrap gap-1.5">
      {chips.map((c, i) => (
        <span
          key={`${c.label}-${i}`}
          className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-[3px] border font-mono text-[11px] tabular ${toneCls[c.tone]}`}
        >
          <span className="text-dim-400 uppercase tracking-[0.1em] text-[9.5px]">
            {c.label}
          </span>
          <span>{c.value}</span>
        </span>
      ))}
    </div>
  );
}

function AllocationVisual({
  legs,
  blendedFormula,
}: {
  legs: AllocationLeg[];
  blendedFormula: string | null;
}) {
  const sum = legs.reduce((s, l) => s + l.pct, 0);
  const sumOk = Math.abs(sum - 100) < 1;
  return (
    <div className="space-y-3">
      <div className="flex h-3 rounded-full overflow-hidden border border-ink-600/60">
        {legs.map((l, i) => (
          <span
            key={`${l.label}-${i}`}
            title={`${l.label} · ${l.pct}%`}
            className="block first:rounded-l-full last:rounded-r-full transition-all"
            style={{ width: `${l.pct}%`, background: l.color }}
          />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1.5 font-mono text-[11.5px]">
        {legs.map((l, i) => (
          <div key={`${l.label}-${i}`} className="flex items-center gap-1.5">
            <span
              className="w-2 h-2 rounded-sm shrink-0"
              style={{ background: l.color }}
            />
            <span className="text-dim-300">{l.label}</span>
            <span className="text-white tabular">{l.pct}%</span>
          </div>
        ))}
        <div className="ml-auto flex items-center gap-1.5">
          <span className={sumOk ? "text-pos" : "text-warn"}>
            {sumOk ? "✓" : "!"}
          </span>
          <span className="text-dim-400 uppercase tracking-[0.12em] text-[10px]">
            Σ
          </span>
          <span
            className={`tabular ${sumOk ? "text-pos" : "text-warn"}`}
          >
            {sum.toFixed(0)}%
          </span>
        </div>
      </div>
      {blendedFormula && (
        <div className="mt-2 font-mono text-[12px] text-dim-300 bg-ink-850/60 border border-ink-600/40 rounded-sm px-3 py-2 tabular">
          Blended APR ={" "}
          <span className="text-white">{blendedFormula}</span>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────── component ───────────────────────────────

export function ThesisView({
  body,
  className = "",
}: {
  body: string;
  className?: string;
}) {
  const [showWorking, setShowWorking] = useState(false);
  const parsed = parseThesis(body);

  // No structure detected → keep raw text but in clean mono prose so
  // long technical strings remain readable.
  const isParseable =
    parsed.regime !== null ||
    parsed.analysis.length > 0 ||
    parsed.allocation !== null ||
    parsed.signals.length > 0;
  if (!isParseable) {
    return (
      <div
        className={`font-sans text-[14px] leading-[1.6] whitespace-pre-wrap text-dim-200 ${className}`}
      >
        {body}
      </div>
    );
  }

  return (
    <div className={`space-y-6 ${className}`}>
      {parsed.signals.length > 0 && (
        <div>
          <ThesisEyebrow>Signals · agent input</ThesisEyebrow>
          <SignalRow chips={parsed.signals} />
        </div>
      )}

      {parsed.regime && (
        <div>
          <ThesisEyebrow>Regime</ThesisEyebrow>
          <p className="text-[14.5px] leading-[1.6] text-white">
            <HighlightedInline text={parsed.regime} />
          </p>
        </div>
      )}

      {parsed.reasoning && (
        <div>
          <ThesisEyebrow>Reasoning</ThesisEyebrow>
          <p className="font-sans text-[13.5px] leading-[1.7] text-dim-200">
            <HighlightedInline text={parsed.reasoning} />
          </p>
        </div>
      )}

      {parsed.analysis.length > 0 && (
        <div>
          <ThesisEyebrow>
            Key analysis · {parsed.analysis.length} picks reviewed
          </ThesisEyebrow>
          <ol className="space-y-2">
            {parsed.analysis.map((item) => (
              <AnalysisRow key={item.index} item={item} />
            ))}
          </ol>
        </div>
      )}

      {parsed.selfCorrection && (
        <div className="bg-ink-850/60 border border-ink-600/50 rounded-md overflow-hidden">
          <button
            type="button"
            onClick={() => setShowWorking((v) => !v)}
            className="w-full flex items-center justify-between gap-3 px-4 py-2.5 text-left hover:bg-ink-800/40 transition-colors"
          >
            <div className="flex items-center gap-2.5">
              <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-warn">
                Show working
              </span>
              <span className="text-[12px] text-dim-300">
                Agent caught and corrected itself — open to see the loop.
              </span>
            </div>
            <span
              className={`font-mono text-[16px] text-warn transition-transform ${showWorking ? "rotate-90" : ""}`}
              aria-hidden
            >
              ▸
            </span>
          </button>
          {showWorking && (
            <div className="px-4 pb-3.5 pt-1 border-t border-ink-600/40 fade-up">
              <p className="font-sans text-[13.5px] leading-[1.65] text-dim-200">
                <HighlightedInline text={parsed.selfCorrection} />
              </p>
            </div>
          )}
        </div>
      )}

      {parsed.allocation && (
        <div>
          <ThesisEyebrow>Decision · final allocation</ThesisEyebrow>
          {parsed.allocationLegs.length > 0 ? (
            <AllocationVisual
              legs={parsed.allocationLegs}
              blendedFormula={parsed.blendedFormula}
            />
          ) : (
            <p className="font-sans text-[13.5px] leading-[1.6] text-white">
              <HighlightedInline text={parsed.allocation} />
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function AnalysisRow({ item }: { item: AnalysisItem }) {
  const verdictClass =
    item.verdict === "REJECT"
      ? "border-danger/30 bg-danger/[0.04]"
      : item.verdict === "KEEP"
        ? "border-pos/30 bg-pos/[0.04]"
        : "border-ink-600/50 bg-ink-900/40";
  const indexClass =
    item.verdict === "REJECT"
      ? "text-danger"
      : item.verdict === "KEEP"
        ? "text-pos"
        : "text-dim-400";

  return (
    <li
      className={`flex gap-3 px-3.5 py-2.5 border rounded-sm ${verdictClass}`}
    >
      <span
        className={`font-mono text-[11px] tabular w-5 flex-none pt-0.5 ${indexClass}`}
      >
        {item.index}.
      </span>
      <span className="font-sans text-[13px] text-dim-200 leading-[1.55] flex-1">
        <HighlightedInline text={item.body} />
      </span>
    </li>
  );
}
