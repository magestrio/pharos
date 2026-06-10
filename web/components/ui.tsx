"use client";

import { useState, type ReactNode, type SVGProps } from "react";

export function fmtUsd(n: number, opts: { decimals?: number; sign?: boolean } = {}): string {
  const { decimals = 2, sign = false } = opts;
  const v = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  const s = n < 0 ? "-" : sign ? "+" : "";
  return s + "$" + v;
}

export function fmtPct(n: number, opts: { decimals?: number; sign?: boolean } = {}): string {
  const { decimals = 2, sign = true } = opts;
  const v = Math.abs(n).toFixed(decimals);
  const s = n < 0 ? "-" : sign ? "+" : "";
  return s + v + "%";
}

export function truncHash(h: string, head = 6, tail = 4): string {
  if (!h) return "";
  if (h.length <= head + tail + 2) return h;
  return h.slice(0, head) + "…" + h.slice(-tail);
}

export function LiveDot({ color = "#F6A94B", size = 8 }: { color?: string; size?: number }) {
  return (
    <span
      className="inline-block rounded-full live-dot"
      style={{ width: size, height: size, background: color }}
      aria-hidden
    />
  );
}

export function HashChip({
  hash,
  head = 6,
  tail = 4,
  className = "",
  label,
}: {
  hash: string;
  head?: number;
  tail?: number;
  className?: string;
  label?: string;
}) {
  const [copied, setCopied] = useState(false);
  const onClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      navigator.clipboard?.writeText(hash);
      setCopied(true);
      setTimeout(() => setCopied(false), 1100);
    } catch {
      // clipboard unavailable
    }
  };
  return (
    <button
      type="button"
      onClick={onClick}
      title={hash}
      className={`group inline-flex items-center gap-1.5 font-mono text-[11px] tracking-tight text-dim-300 hover:text-white transition-colors ${className}`}
    >
      {label && <span className="text-dim-500">{label}</span>}
      <span className="border-b border-dashed border-ink-500 group-hover:border-neon">
        {truncHash(hash, head, tail)}
      </span>
      <svg
        width="11"
        height="11"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        className="opacity-50 group-hover:opacity-100"
      >
        <rect x="9" y="9" width="13" height="13" rx="2" />
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
      </svg>
      {copied && <span className="text-neon">copied</span>}
    </button>
  );
}

// `green` is kept as an alias on the amber tone — existing callsites
// using `tone="green"` for "active / agent / live" semantics keep working
// and inherit the new brand colour. `pos` is the new explicit tone for
// "positive delta" (price up, profitable cycle) using teal so up/down
// stays semantically distinct from brand.
type Tone =
  | "neutral"
  | "accent"
  | "green"
  | "pos"
  | "blue"
  | "red"
  | "warn"
  | "mono";

export function Tag({
  children,
  tone = "neutral",
  className = "",
}: {
  children: ReactNode;
  tone?: Tone;
  className?: string;
}) {
  const tones: Record<Tone, string> = {
    neutral: "bg-ink-700/60 text-dim-300 border-ink-500/60",
    accent: "bg-accent/10 text-accent border-accent/30",
    green: "bg-accent/10 text-accent border-accent/30",
    pos: "bg-pos/10 text-pos border-pos/30",
    blue: "bg-elec/10 text-elec border-elec/30",
    red: "bg-danger/10 text-danger border-danger/30",
    warn: "bg-warn/10 text-warn border-warn/30",
    mono: "bg-ink-800 text-dim-300 border-ink-600 font-mono",
  };
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-sm text-[10.5px] uppercase tracking-[0.08em] border ${tones[tone]} ${className}`}
    >
      {children}
    </span>
  );
}

/**
 * Small mono-uppercase label used above headlines, beside numbers, on
 * card tops. Replaces ad-hoc `text-[10.5px] uppercase tracking-...` spans
 * scattered across the codebase.
 */
export function Eyebrow({
  children,
  className = "",
  tone = "neutral",
}: {
  children: ReactNode;
  className?: string;
  tone?: "neutral" | "accent" | "dim";
}) {
  const toneCls =
    tone === "accent" ? "text-accent" : tone === "dim" ? "text-dim-500" : "text-dim-400";
  return (
    <div
      className={`font-mono text-[10.5px] uppercase tracking-[0.18em] ${toneCls} ${className}`}
    >
      {children}
    </div>
  );
}

/**
 * Single primitive for all CTAs. Three variants, one shape system, so
 * buttons across the app share weight/spacing/typography. `primary` is
 * the brand amber pill with subtle inner highlight and an outer halo
 * appearing on hover; `secondary` is the ghost outline; `terminal` is
 * the data-control variant used for filters and expand/collapse.
 */
export function Button({
  children,
  variant = "primary",
  href,
  onClick,
  type,
  className = "",
  active = false,
  size,
}: {
  children: ReactNode;
  variant?: "primary" | "secondary" | "terminal" | "ghost";
  href?: string;
  onClick?: () => void;
  type?: "button" | "submit";
  className?: string;
  active?: boolean;
  size?: "sm" | "md" | "lg";
}) {
  const heightBy = {
    primary: size === "lg" ? "h-12" : "h-11",
    secondary: size === "lg" ? "h-12" : "h-11",
    terminal: size === "sm" ? "h-8" : "h-9",
    ghost: "h-8",
  } as const;

  const base =
    "group relative inline-flex items-center justify-center gap-2 select-none font-mono uppercase tracking-[0.12em] text-[12.5px] transition-all";

  const variantCls: Record<typeof variant, string> = {
    primary: `bg-accent text-[#1B1300] font-semibold px-5 rounded-[3px] shadow-[inset_0_1px_0_rgba(255,255,255,0.30),0_0_0_1px_rgba(246,169,75,0.6),0_8px_24px_-10px_rgba(246,169,75,0.45)] hover:bg-accent-soft hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.35),0_0_0_1px_rgba(255,201,122,0.7),0_10px_30px_-8px_rgba(246,169,75,0.55)] active:translate-y-px ${heightBy.primary}`,
    secondary: `bg-transparent border border-ink-500 text-white px-5 rounded-[3px] hover:border-accent/60 hover:bg-accent/[0.06] hover:text-accent active:translate-y-px ${heightBy.secondary}`,
    terminal: `border border-ink-600/70 bg-ink-900 text-dim-300 px-3 rounded-[3px] hover:text-white hover:bg-ink-800 ${
      active
        ? "border-accent/50 bg-accent/[0.07] text-accent hover:bg-accent/[0.09] hover:text-accent"
        : ""
    } ${heightBy.terminal}`,
    ghost: `text-dim-400 hover:text-accent normal-case tracking-normal text-[12px] px-1 ${heightBy.ghost}`,
  };

  const cls = `${base} ${variantCls[variant]} ${className}`;

  if (href) {
    return (
      <a href={href} className={cls}>
        {children}
      </a>
    );
  }
  return (
    <button type={type ?? "button"} onClick={onClick} className={cls}>
      {children}
    </button>
  );
}

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`bg-ink-900 border border-ink-600/70 rounded-md ${className}`}>{children}</div>
  );
}

/**
 * Pulsing rectangle used as a content placeholder while a query is in
 * flight. Caller controls the geometry via className/style; this keeps
 * the primitive lightweight + theme-consistent.
 */
export function SkeletonBox({
  className = "",
  style,
}: {
  className?: string;
  style?: React.CSSProperties;
}) {
  return <div className={`bg-ink-700/60 animate-pulse rounded-sm ${className}`} style={style} />;
}

/**
 * One-line skeleton row matching the typography of a typical data row.
 * Used for list previews while React Query is still resolving.
 */
export function SkeletonRow({ width = "100%" }: { width?: string | number }) {
  return <SkeletonBox className="h-4" style={{ width }} />;
}

/**
 * Inline panel rendered when a query has failed. Keeps a warn-tinted
 * border + plain-text message so the app never crashes on a transient
 * fetch error; callers pass a short label + the raw message.
 */
export function ErrorPanel({
  label,
  message,
  className = "",
}: {
  label: string;
  message?: string;
  className?: string;
}) {
  return (
    <div
      className={`border border-danger/40 bg-danger/5 rounded-sm px-3 py-2.5 ${className}`}
    >
      <div className="font-mono text-[12px] text-danger">{label}</div>
      {message && (
        <div className="mt-1 font-mono text-[11px] text-dim-400 leading-snug break-words">
          {message}
        </div>
      )}
    </div>
  );
}

type StatTone = "neutral" | "accent" | "green" | "pos" | "blue" | "red";

export function StatCard({
  label,
  value,
  suffix,
  sub,
  tone = "neutral",
  mono = true,
}: {
  label: string;
  value: ReactNode;
  suffix?: ReactNode;
  sub?: ReactNode;
  tone?: StatTone;
  mono?: boolean;
}) {
  // `green` aliased to `accent` so legacy callsites pick up the new amber
  // hue. `pos` is teal — reserved for explicit positive-delta semantics.
  const toneCls: Record<StatTone, string> = {
    neutral: "text-white",
    accent: "text-accent",
    green: "text-accent",
    pos: "text-pos",
    blue: "text-elec",
    red: "text-danger",
  };
  return (
    <div
      className="relative overflow-hidden bg-gradient-to-b from-ink-850 to-ink-900 border border-ink-600/70 rounded-md p-4 sm:p-5 shadow-card-lift
                 before:content-[''] before:absolute before:inset-x-0 before:top-0 before:h-px before:bg-gradient-to-r before:from-transparent before:via-white/[0.07] before:to-transparent"
    >
      <Eyebrow tone="dim" className="mb-2 flex items-center justify-between">
        <span>{label}</span>
      </Eyebrow>
      <div
        className={`${mono ? "font-mono" : "font-serif"} text-2xl sm:text-[30px] leading-none font-semibold tabular ${toneCls[tone]}`}
      >
        {value}
        {suffix && (
          <span className="text-dim-400 text-base ml-0.5 font-normal font-sans">{suffix}</span>
        )}
      </div>
      {sub && <div className="mt-2 text-[11px] text-dim-400 font-mono tabular">{sub}</div>}
    </div>
  );
}

export function DonutChart({
  data,
  size = 200,
  thickness = 22,
  gap = 0.02,
  centerLabel,
  centerValue,
}: {
  data: Array<{ pct: number; color: string; label?: string }>;
  size?: number;
  thickness?: number;
  gap?: number;
  centerLabel?: string;
  centerValue?: string;
}) {
  const total = data.reduce((s, d) => s + d.pct, 0);
  const r = size / 2 - thickness / 2;
  const C = 2 * Math.PI * r;
  let acc = 0;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="overflow-visible">
      <circle cx={size / 2} cy={size / 2} r={r} stroke="#161D38" strokeWidth={thickness} fill="none" />
      {data.map((d, i) => {
        const frac = d.pct / total;
        const len = C * frac - C * gap;
        const offset = -C * (acc / total) + C * (gap / 2);
        acc += d.pct;
        return (
          <circle
            key={i}
            cx={size / 2}
            cy={size / 2}
            r={r}
            stroke={d.color}
            strokeWidth={thickness}
            fill="none"
            strokeDasharray={`${Math.max(len, 0)} ${C}`}
            strokeDashoffset={offset}
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
            strokeLinecap="butt"
          />
        );
      })}
      {centerValue && (
        <g>
          <text
            x={size / 2}
            y={size / 2 - 4}
            textAnchor="middle"
            fill="#E6EAF2"
            fontFamily="JetBrains Mono, monospace"
            fontSize="22"
            fontWeight="600"
          >
            {centerValue}
          </text>
          <text
            x={size / 2}
            y={size / 2 + 16}
            textAnchor="middle"
            fill="#7A8499"
            fontFamily="Inter, sans-serif"
            fontSize="10"
            letterSpacing="2"
          >
            {centerLabel}
          </text>
        </g>
      )}
    </svg>
  );
}

export function LineChart({
  series,
  width = 600,
  height = 220,
  color = "#F6A94B",
  baseline = 1000,
  pad = { t: 16, r: 16, b: 24, l: 40 },
  showAxis = true,
  showGrid = true,
  label = "",
  compareSeries = null,
  compareColor = "#7A8499",
  yFormat = null,
  baselineLabel = null,
}: {
  series: number[];
  width?: number;
  height?: number;
  color?: string;
  fillColor?: string;
  baseline?: number;
  pad?: { t: number; r: number; b: number; l: number };
  showAxis?: boolean;
  showGrid?: boolean;
  label?: string;
  compareSeries?: number[] | null;
  compareColor?: string;
  yFormat?: ((v: number) => string) | null;
  baselineLabel?: string | null;
}) {
  const fmt =
    yFormat ||
    ((v: number) => {
      if (Math.abs(v) >= 1_000_000) return "$" + (v / 1_000_000).toFixed(2) + "M";
      if (Math.abs(v) >= 1000) return "$" + Math.round(v / 1000) + "k";
      if (v > 0 && v < 10) return v.toFixed(4);
      return Math.round(v).toString();
    });
  const baseLbl = baselineLabel != null ? baselineLabel : fmt(baseline);
  const allValues = [...series, ...(compareSeries || []), baseline];
  const min = Math.min(...allValues);
  const max = Math.max(...allValues);
  const padY = (max - min) * 0.12 || 1;
  const yMin = min - padY;
  const yMax = max + padY;
  const innerW = width - pad.l - pad.r;
  const innerH = height - pad.t - pad.b;
  const xAt = (i: number) => pad.l + innerW * (i / (series.length - 1));
  const yAt = (v: number) => pad.t + innerH - ((v - yMin) / (yMax - yMin)) * innerH;

  const pathD = series
    .map((v, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`)
    .join(" ");
  const fillD =
    pathD +
    ` L${xAt(series.length - 1).toFixed(1)},${yAt(yMin).toFixed(1)} L${xAt(0).toFixed(1)},${yAt(yMin).toFixed(1)} Z`;
  const compareD = compareSeries
    ? compareSeries
        .map((v, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`)
        .join(" ")
    : null;

  const ticks = 4;
  const grid: Array<{ v: number; y: number }> = [];
  for (let i = 0; i <= ticks; i++) {
    const v = yMin + (yMax - yMin) * (i / ticks);
    grid.push({ v, y: yAt(v) });
  }

  const xLabels: Array<{ i: number; x: number; label: string }> = [];
  const step = Math.ceil((series.length - 1) / 6);
  for (let i = 0; i < series.length; i += step) {
    xLabels.push({ i, x: xAt(i), label: `D${i}` });
  }
  if (xLabels[xLabels.length - 1].i !== series.length - 1) {
    xLabels.push({ i: series.length - 1, x: xAt(series.length - 1), label: `D${series.length - 1}` });
  }

  const last = series[series.length - 1];
  const baseY = yAt(baseline);
  const gradId = `fillgrad-${label}`;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="w-full h-full block">
      <defs>
        <linearGradient id={gradId} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.22" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>

      {showGrid &&
        grid.map((g, i) => (
          <line
            key={i}
            x1={pad.l}
            x2={width - pad.r}
            y1={g.y}
            y2={g.y}
            stroke="#1C2442"
            strokeDasharray="2 4"
            strokeWidth="1"
          />
        ))}

      <line
        x1={pad.l}
        x2={width - pad.r}
        y1={baseY}
        y2={baseY}
        stroke="#3A4567"
        strokeDasharray="3 3"
        strokeWidth="1"
      />
      <text
        x={pad.l - 4}
        y={baseY + 3}
        textAnchor="end"
        fill="#5B6679"
        fontSize="9"
        fontFamily="JetBrains Mono, monospace"
      >
        {baseLbl}
      </text>

      {showAxis &&
        grid.map((g, i) => (
          <text
            key={i}
            x={pad.l - 6}
            y={g.y + 3}
            textAnchor="end"
            fill="#5B6679"
            fontSize="9"
            fontFamily="JetBrains Mono, monospace"
          >
            {fmt(g.v)}
          </text>
        ))}

      {showAxis &&
        xLabels.map((xl, i) => (
          <text
            key={i}
            x={xl.x}
            y={height - 6}
            textAnchor="middle"
            fill="#5B6679"
            fontSize="9"
            fontFamily="JetBrains Mono, monospace"
          >
            {xl.label}
          </text>
        ))}

      {compareD && (
        <path d={compareD} fill="none" stroke={compareColor} strokeWidth="1.25" strokeDasharray="3 3" opacity="0.55" />
      )}

      <path d={fillD} fill={`url(#${gradId})`} />
      <path
        d={pathD}
        fill="none"
        stroke={color}
        strokeWidth="1.75"
        strokeLinejoin="round"
        strokeLinecap="round"
      />

      <circle cx={xAt(series.length - 1)} cy={yAt(last)} r="3.5" fill={color} />
      <circle cx={xAt(series.length - 1)} cy={yAt(last)} r="7" fill={color} fillOpacity="0.18" />
    </svg>
  );
}

export function Sparkline({
  series,
  color = "#F6A94B",
  width = 120,
  height = 28,
}: {
  series: number[];
  color?: string;
  width?: number;
  height?: number;
}) {
  if (!series || !series.length) return null;
  const min = Math.min(...series);
  const max = Math.max(...series);
  const span = max - min || 1;
  const pts = series
    .map((v, i) => {
      const x = (i / (series.length - 1)) * width;
      const y = height - ((v - min) / span) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="block">
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.2" />
    </svg>
  );
}

export function Ticker({
  items,
}: {
  items: Array<{ k: string; v: string; d: string; up: boolean }>;
}) {
  const doubled = [...items, ...items];
  return (
    <div className="border-y border-ink-600/60 bg-ink-900/60 backdrop-blur no-scroll-x">
      <div className="ticker-track flex gap-10 py-2.5 whitespace-nowrap">
        {doubled.map((it, i) => (
          <div key={i} className="flex items-center gap-2 text-[11px] font-mono shrink-0">
            <span className="text-dim-500 uppercase tracking-[0.14em]">{it.k}</span>
            <span className="text-white tabular">{it.v}</span>
            <span className={`tabular ${it.up ? "text-pos" : "text-danger"}`}>{it.d}</span>
            <span className="text-accent/25 px-1">│</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function SectionHead({
  eyebrow,
  title,
  subtitle,
  right,
}: {
  eyebrow?: ReactNode;
  title?: ReactNode;
  subtitle?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-4 mb-5">
      <div>
        {eyebrow && <Eyebrow tone="accent" className="mb-2">{eyebrow}</Eyebrow>}
        {title && (
          <div className="font-serif text-[26px] sm:text-[32px] leading-[1.05] tracking-[-0.015em] text-white">
            {title}
          </div>
        )}
        {subtitle && (
          <div className="text-[14px] sm:text-[15px] text-dim-400 mt-2 max-w-2xl leading-[1.55]">
            {subtitle}
          </div>
        )}
      </div>
      {right}
    </div>
  );
}

type IconProps = SVGProps<SVGSVGElement>;

// All icons share a 1.5px stroke for visual consistency. Earlier the set
// mixed 1.6/1.8/2.0/2.2/2.4 which read as inconsistent next to the new
// editorial typography.
const ICON_STROKE = "1.5";

export const Icon = {
  Wallet: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M21 12V7a2 2 0 0 0-2-2H5a2 2 0 0 0 0 4h16v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7" />
      <path d="M17 12h.01" />
    </svg>
  ),
  Arrow: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M5 12h14M13 5l7 7-7 7" />
    </svg>
  ),
  Ext: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M7 17 17 7M9 7h8v8" />
    </svg>
  ),
  Check: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M5 12l5 5L20 7" />
    </svg>
  ),
  Chev: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="m9 18 6-6-6-6" />
    </svg>
  ),
  Spinner: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" {...p}>
      <path d="M21 12a9 9 0 1 1-6.219-8.56" />
    </svg>
  ),
  Filter: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z" />
    </svg>
  ),
  Search: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" {...p}>
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.3-4.3" />
    </svg>
  ),
  Robot: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" {...p}>
      <rect x="3" y="8" width="18" height="12" rx="2" />
      <path d="M12 8V4M9 4h6" />
      <circle cx="9" cy="14" r="1.5" />
      <circle cx="15" cy="14" r="1.5" />
    </svg>
  ),
  User: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" {...p}>
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21a8 8 0 0 1 16 0" />
    </svg>
  ),
  Sigma: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M6 4h12v3L12 12l6 5v3H6" />
    </svg>
  ),
  Bolt: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" />
    </svg>
  ),
  Block: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="m12 2 9 4.9v10.2L12 22 3 17.1V6.9z" />
      <path d="M3.27 6.96 12 12.01l8.73-5.05M12 22.08V12" />
    </svg>
  ),
  Sparkle: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.5 5.5l2.8 2.8M15.7 15.7l2.8 2.8M5.5 18.5l2.8-2.8M15.7 8.3l2.8-2.8" />
    </svg>
  ),
};
