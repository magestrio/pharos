import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Inter, JetBrains_Mono } from "next/font/google";

import "./globals.css";
import { Providers } from "./providers";

const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-sans",
  display: "swap",
});

const jetbrains = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Vault8004 — vUSDC, AI-Managed Yield-Bearing USDC Wrapper",
  description:
    "AI yield vault on Mantle. Mint USDC, receive vUSDC. Every decision logged on-chain. Reputation verifiable through ERC-8004.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${jetbrains.variable}`}>
      <body className="min-h-screen bg-ink-950 text-[#E6EAF2]">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
