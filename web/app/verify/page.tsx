/**
 * Judge verification surface (`frontend-complete.18`).
 *
 * Single page bundling every external verification path:
 *   - Contract addresses on Mantle Explorer
 *   - Last DecisionLog events with IPFS rationale + tx links
 *   - Last reputation update with tokenId, score, tx
 *   - Safe owner verification (2-of-3 multisig)
 *
 * Thin Server Component shell; the data sections are client-side
 * (wagmi reads + getLogs over Mantle).
 */
import Link from "next/link";

import { VerifyPage } from "@/components/verify-page";

export const dynamic = "force-dynamic";
export const metadata = {
  title: "Pharos - Judge Verification Surface",
  description:
    "All on-chain verification paths for Pharos in one page: contract addresses, decision log proofs, reputation history, attestor Safe.",
};

export default function VerifyRoute() {
  return (
    <main className="min-h-screen bg-ink-950 text-white">
      <header className="border-b border-ink-600/60 bg-ink-950/90 backdrop-blur sticky top-0 z-10">
        <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 h-14 flex items-center justify-between">
          <Link
            href="/"
            className="font-mono text-[13px] text-white font-semibold tracking-tight hover:text-neon"
          >
            ← PHAROS
          </Link>
          <div className="font-mono text-[11px] uppercase tracking-[0.14em] text-dim-500">
            Judge Verification Surface
          </div>
        </div>
      </header>

      <section className="max-w-[1180px] mx-auto px-4 sm:px-6 lg:px-8 py-8 sm:py-10">
        <VerifyPage />
      </section>
    </main>
  );
}
