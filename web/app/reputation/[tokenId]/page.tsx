/**
 * ERC-8004 reputation card page (`frontend-complete.15`).
 *
 * Dedicated route per agent NFT — score history, current value via
 * ReputationOracle, links to canonical registries on Mantle Explorer,
 * leaderboard placeholder. Reads are entirely on-chain so this is a
 * thin Server Component shell wrapping the client-side reputation
 * page component.
 */
import Link from "next/link";

import { ReputationPage } from "@/components/reputation-page";

export const dynamic = "force-dynamic";
export const metadata = { title: "Pharos — ERC-8004 Reputation" };

export default function ReputationTokenPage({
  params,
}: {
  params: { tokenId: string };
}) {
  const tokenId = parseTokenId(params.tokenId);
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
            ERC-8004 Reputation · Token #{tokenId.toString()}
          </div>
        </div>
      </header>

      <section className="max-w-[1180px] mx-auto px-4 sm:px-6 lg:px-8 py-8 sm:py-10">
        <ReputationPage tokenId={tokenId} />
      </section>
    </main>
  );
}

function parseTokenId(raw: string): bigint {
  try {
    return BigInt(raw);
  } catch {
    return 0n;
  }
}
