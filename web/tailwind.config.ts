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
        neon: {
          DEFAULT: "#00FF88",
          soft: "#22E37A",
          dim: "#0E8F4F",
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
        "glow-green": "0 0 0 1px rgba(0,255,136,0.35), 0 0 24px -4px rgba(0,255,136,0.35)",
        "glow-blue": "0 0 0 1px rgba(91,143,249,0.35), 0 0 24px -4px rgba(91,143,249,0.35)",
      },
    },
  },
  plugins: [],
};

export default config;
