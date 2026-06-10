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
        // Twilight neutrals - warm deep-indigo (not near-black). Lower
        // numbers are lighter. Lifted off pure black so dense data reads
        // softer; hue leans indigo to match the dusk/beacon theme.
        ink: {
          400: "#3A4567",
          500: "#2C3658",
          600: "#232C4C",
          700: "#1C2442",
          750: "#161D38",
          800: "#131A33",
          850: "#10162C",
          900: "#0E1326",
          950: "#0B0E1C",
        },
        dim: {
          300: "#A6AFC2",
          400: "#7A8499",
          500: "#5B6679",
          600: "#3F4860",
        },
        // Brand primary - soft sunset gold (Twilight beacon light).
        // `neon` is kept as a name-alias so legacy `text-neon` / `bg-neon`
        // callsites pick up the new hue without a 50-file rename.
        accent: {
          DEFAULT: "#F6A94B",
          soft: "#FFC97A",
          dim: "#9A6A2A",
          ink: "#1B1300",
        },
        neon: {
          DEFAULT: "#F6A94B",
          soft: "#FFC97A",
          dim: "#9A6A2A",
        },
        // Positive-delta indicator (price up, profitable cycle). Kept
        // separate from `accent` so gold stays a brand colour and green
        // never overloads it semantically.
        pos: {
          DEFAULT: "#4ADE80",
          soft: "#86EFAC",
          dim: "#15803D",
        },
        // Secondary "beam" accent - lavender. Repurposed from the old
        // electric-blue `elec` token (callsites unchanged); pairs the warm
        // gold beacon with a cool dusk sweep.
        elec: {
          DEFAULT: "#A78BFA",
          soft: "#C4B5FD",
          dim: "#7C5CD6",
        },
        danger: "#FB7185",
        warn: "#FBBF6B",
      },
      boxShadow: {
        "glow-amber":
          "0 0 0 1px rgba(246,169,75,0.35), 0 0 28px -4px rgba(246,169,75,0.40)",
        "glow-green":
          "0 0 0 1px rgba(74,222,128,0.30), 0 0 24px -4px rgba(74,222,128,0.30)",
        "glow-blue":
          "0 0 0 1px rgba(167,139,250,0.35), 0 0 24px -4px rgba(167,139,250,0.35)",
        "card-lift":
          "0 1px 0 rgba(255,255,255,0.04) inset, 0 8px 24px -12px rgba(0,0,0,0.6)",
        "card-premium":
          "0 1px 0 rgba(255,255,255,0.06) inset, 0 0 0 1px rgba(246,169,75,0.10), 0 20px 60px -20px rgba(0,0,0,0.7)",
      },
    },
  },
  plugins: [],
};

export default config;
