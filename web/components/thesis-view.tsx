/**
 * Structured render of an agent decision thesis.
 *
 * The Claude prompt yields a fairly consistent format: an opening
 * "regime" paragraph, an explicit `Key analysis:` numbered list where
 * each item ends in `KEEP` or `REJECT`, and a closing
 * `Allocation:` summary line. This component parses that into
 * three sections with REJECT/KEEP color coding + number/product
 * highlighting. Anything that doesn't fit the format falls back to
 * a plain pre-wrap render so older / weirder theses still display.
 */

import React from "react";

type Verdict = "KEEP" | "REJECT" | null;

interface AnalysisItem {
  index: number;
  body: string;
  verdict: Verdict;
}

interface ParsedThesis {
  regime: string | null;
  analysis: AnalysisItem[];
  allocation: string | null;
  // Anything left over that didn't fit (preserved verbatim).
  trailing: string | null;
}

const HEADER_KEY_ANALYSIS = /^key analysis:?$/i;
const HEADER_ALLOCATION = /^allocation:?\s*(.*)$/i;
const NUMBERED = /^(\d+)\.\s+(.*)$/;

function parseThesis(raw: string): ParsedThesis {
  const lines = raw.split(/\r?\n/);
  let mode: "regime" | "analysis" | "allocation" | "trailing" = "regime";
  const regimeBuf: string[] = [];
  const analysis: AnalysisItem[] = [];
  let allocation: string | null = null;
  const trailingBuf: string[] = [];

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
      // Blank line — flushes a pending list item.
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
      allocation = allocMatch[1].trim() || null;
      continue;
    }

    if (mode === "regime") {
      regimeBuf.push(line);
    } else if (mode === "analysis") {
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
        // Stray line between header and the first item.
        trailingBuf.push(line);
      }
    } else if (mode === "allocation") {
      // Allocation header may be followed by extra notes.
      if (allocation) {
        allocation += " " + line.trim();
      } else {
        allocation = line.trim();
      }
    } else {
      trailingBuf.push(line);
    }
  }
  flushItem();

  return {
    regime: regimeBuf.join(" ").trim() || null,
    analysis,
    allocation: allocation?.trim() || null,
    trailing: trailingBuf.join("\n").trim() || null,
  };
}

function detectVerdict(s: string): Verdict {
  // Verdict tokens appear at the tail of the line: "... → REJECT." or
  // "... KEEP." Case-insensitive, optional arrow / trailing dot.
  const tail = s.trim().toUpperCase().replace(/\.$/, "").trim();
  if (tail.endsWith("REJECT") || /\bREJECT\b\s*$/.test(tail)) return "REJECT";
  if (tail.endsWith("KEEP") || /\bKEEP\b\s*$/.test(tail)) return "KEEP";
  return null;
}

// ─────────────────────────── inline tokens ───────────────────────────

// Tokenize body text to inject colored spans. Matches percentages,
// dollar amounts, REJECT/KEEP keywords, and venue/product references
// like "OnChain (product 9)" or "ATOM OnChain". Keep the regex
// conservative — when in doubt, leave plain.
const TOKEN_RX = new RegExp(
  [
    String.raw`(\bREJECT\b)`,
    String.raw`(\bKEEP\b)`,
    String.raw`([+-]?\d+(?:\.\d+)?%)`, // percentages
    String.raw`(\$\d+(?:,\d{3})*(?:\.\d+)?)`, // $1,234.56
    String.raw`(\bfunding_rate_[a-z0-9_]+\b)`, // funding_rate_7d_avg
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
      <span key={key} className="text-neon font-semibold">
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
        ? "text-neon"
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
  // Venue / coin keywords — subtle off-white for scannability.
  return (
    <span key={key} className="text-white/90">
      {tok}
    </span>
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
  const parsed = parseThesis(body);

  // No structure detected → preserve the original whitespace render so
  // older theses still readable.
  const isParseable =
    parsed.regime !== null ||
    parsed.analysis.length > 0 ||
    parsed.allocation !== null;
  if (!isParseable) {
    return (
      <div
        className={`text-[13px] leading-relaxed whitespace-pre-wrap text-dim-200 ${className}`}
      >
        {body}
      </div>
    );
  }

  return (
    <div className={`space-y-5 ${className}`}>
      {parsed.regime && (
        <Section eyebrow="Regime">
          <p className="text-[13px] text-dim-200 leading-relaxed">
            <HighlightedInline text={parsed.regime} />
          </p>
        </Section>
      )}

      {parsed.analysis.length > 0 && (
        <Section eyebrow={`Key analysis · ${parsed.analysis.length} picks reviewed`}>
          <ol className="space-y-2">
            {parsed.analysis.map((item) => (
              <AnalysisRow key={item.index} item={item} />
            ))}
          </ol>
        </Section>
      )}

      {parsed.allocation && (
        <Section eyebrow="Allocation">
          <p className="text-[13px] text-white leading-relaxed">
            <HighlightedInline text={parsed.allocation} />
          </p>
        </Section>
      )}

      {parsed.trailing && (
        <Section eyebrow="Notes">
          <p className="text-[12.5px] text-dim-300 leading-relaxed whitespace-pre-wrap">
            {parsed.trailing}
          </p>
        </Section>
      )}
    </div>
  );
}

function Section({
  eyebrow,
  children,
}: {
  eyebrow: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="text-[10px] font-mono uppercase tracking-[0.16em] text-dim-500 mb-2">
        {eyebrow}
      </div>
      {children}
    </div>
  );
}

function AnalysisRow({ item }: { item: AnalysisItem }) {
  const verdictClass =
    item.verdict === "REJECT"
      ? "border-danger/30 bg-danger/[0.04]"
      : item.verdict === "KEEP"
      ? "border-neon/30 bg-neon/[0.04]"
      : "border-ink-600/50 bg-ink-900/40";
  const indexClass =
    item.verdict === "REJECT"
      ? "text-danger"
      : item.verdict === "KEEP"
      ? "text-neon"
      : "text-dim-400";

  return (
    <li
      className={`flex gap-3 px-3 py-2 border rounded-sm ${verdictClass}`}
    >
      <span
        className={`font-mono text-[11px] tabular w-5 flex-none pt-0.5 ${indexClass}`}
      >
        {item.index}.
      </span>
      <span className="text-[12.5px] text-dim-200 leading-relaxed flex-1">
        <HighlightedInline text={item.body} />
      </span>
    </li>
  );
}
