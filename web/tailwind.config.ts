import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./lib/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "SFMono-Regular", "monospace"],
        serif: ["var(--font-serif)", "Fraunces", "ui-serif", "Georgia", "serif"],
      },
      colors: {
        ink: {
          400: "#2F3A55",
          500: "#222B44",
          600: "#1A2138",
          700: "#161C32",
          750: "#11172A",
          800: "#0E1322",
          850: "#0C1120",
          900: "#0A0E1A",
          950: "#070A14",
        },
        dim: {
          300: "#A6AFC2",
          400: "#7A8499",
          500: "#5B6679",
          600: "#3F4860",
        },
        // Brand primary — warm amber / honey gold.
        // `neon` is kept as a name-alias so legacy `text-neon` / `bg-neon`
        // callsites pick up the new hue without a 50-file rename.
        accent: {
          DEFAULT: "#F5B400",
          soft: "#FFC533",
          dim: "#8A6500",
          ink: "#1B1300",
        },
        neon: {
          DEFAULT: "#F5B400",
          soft: "#FFC533",
          dim: "#8A6500",
        },
        // Positive-delta indicator (price up, profitable cycle). Kept
        // separate from `accent` so amber stays a brand colour and green
        // never overloads it semantically.
        pos: {
          DEFAULT: "#34D399",
          soft: "#6EE7B7",
          dim: "#047857",
        },
        elec: {
          DEFAULT: "#5B8FF9",
          soft: "#7AA5FB",
          dim: "#345FC2",
        },
        danger: "#FF5470",
        warn: "#F7B955",
      },
      boxShadow: {
        "glow-amber":
          "0 0 0 1px rgba(245,180,0,0.35), 0 0 28px -4px rgba(245,180,0,0.40)",
        "glow-green":
          "0 0 0 1px rgba(245,180,0,0.35), 0 0 24px -4px rgba(245,180,0,0.35)",
        "glow-blue":
          "0 0 0 1px rgba(91,143,249,0.35), 0 0 24px -4px rgba(91,143,249,0.35)",
        "card-lift":
          "0 1px 0 rgba(255,255,255,0.04) inset, 0 8px 24px -12px rgba(0,0,0,0.6)",
        "card-premium":
          "0 1px 0 rgba(255,255,255,0.06) inset, 0 0 0 1px rgba(245,180,0,0.10), 0 20px 60px -20px rgba(0,0,0,0.7)",
      },
    },
  },
  plugins: [],
};

export default config;
