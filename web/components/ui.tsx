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

export function LiveDot({ color = "#00FF88", size = 8 }: { color?: string; size?: number }) {
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

type Tone = "neutral" | "green" | "blue" | "red" | "warn" | "mono";

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
    green: "bg-neon/10 text-neon border-neon/30",
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

type StatTone = "neutral" | "green" | "blue" | "red";

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
  const toneCls: Record<StatTone, string> = {
    neutral: "text-white",
    green: "text-neon",
    blue: "text-elec",
    red: "text-danger",
  };
  return (
    <div className="bg-ink-900 border border-ink-600/70 rounded-md p-4 sm:p-5 relative overflow-hidden">
      <div className="text-[10.5px] uppercase tracking-[0.16em] text-dim-500 mb-2 flex items-center justify-between">
        <span>{label}</span>
      </div>
      <div
        className={`${mono ? "font-mono" : ""} text-2xl sm:text-[28px] leading-none font-semibold tabular ${toneCls[tone]}`}
      >
        {value}
        {suffix && <span className="text-dim-400 text-base ml-0.5 font-normal">{suffix}</span>}
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
      <circle cx={size / 2} cy={size / 2} r={r} stroke="#11172A" strokeWidth={thickness} fill="none" />
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
  color = "#00FF88",
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
            stroke="#161C32"
            strokeDasharray="2 4"
            strokeWidth="1"
          />
        ))}

      <line
        x1={pad.l}
        x2={width - pad.r}
        y1={baseY}
        y2={baseY}
        stroke="#2F3A55"
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
  color = "#00FF88",
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
    <div className="border-y border-ink-600/60 bg-ink-900/80 backdrop-blur no-scroll-x">
      <div className="ticker-track flex gap-8 py-2 whitespace-nowrap">
        {doubled.map((it, i) => (
          <div key={i} className="flex items-center gap-2 text-[11px] font-mono shrink-0">
            <span className="text-dim-500 uppercase tracking-[0.12em]">{it.k}</span>
            <span className="text-white tabular">{it.v}</span>
            <span className={`tabular ${it.up ? "text-neon" : "text-danger"}`}>{it.d}</span>
            <span className="text-ink-500">|</span>
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
    <div className="flex items-end justify-between gap-4 mb-4">
      <div>
        {eyebrow && (
          <div className="text-[10.5px] uppercase tracking-[0.18em] text-dim-500 font-mono mb-1.5">{eyebrow}</div>
        )}
        {title && <div className="text-lg sm:text-xl text-white font-semibold">{title}</div>}
        {subtitle && <div className="text-sm text-dim-400 mt-1 max-w-2xl">{subtitle}</div>}
      </div>
      {right}
    </div>
  );
}

type IconProps = SVGProps<SVGSVGElement>;

export const Icon = {
  Wallet: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M21 12V7a2 2 0 0 0-2-2H5a2 2 0 0 0 0 4h16v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7" />
      <path d="M17 12h.01" />
    </svg>
  ),
  Arrow: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M5 12h14M13 5l7 7-7 7" />
    </svg>
  ),
  Ext: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M7 17 17 7M9 7h8v8" />
    </svg>
  ),
  Check: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M5 12l5 5L20 7" />
    </svg>
  ),
  Chev: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="m9 18 6-6-6-6" />
    </svg>
  ),
  Spinner: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" {...p}>
      <path d="M21 12a9 9 0 1 1-6.219-8.56" />
    </svg>
  ),
  Filter: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z" />
    </svg>
  ),
  Search: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.3-4.3" />
    </svg>
  ),
  Robot: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <rect x="3" y="8" width="18" height="12" rx="2" />
      <path d="M12 8V4M9 4h6" />
      <circle cx="9" cy="14" r="1.5" />
      <circle cx="15" cy="14" r="1.5" />
    </svg>
  ),
  User: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21a8 8 0 0 1 16 0" />
    </svg>
  ),
  Sigma: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M6 4h12v3L12 12l6 5v3H6" />
    </svg>
  ),
  Bolt: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" />
    </svg>
  ),
  Block: (p: IconProps) => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}>
      <path d="m12 2 9 4.9v10.2L12 22 3 17.1V6.9z" />
      <path d="M3.27 6.96 12 12.01l8.73-5.05M12 22.08V12" />
    </svg>
  ),
};
