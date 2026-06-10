/**
 * Pharos home page (`frontend-complete.5`).
 *
 * Server Component - fetches the cycle history list + current
 * portfolio from the agent FastAPI once per request, then hands the
 * results to the client-side `<Shell>` as initial data. Subsequent
 * revalidation happens client-side via React Query (30s interval +
 * refetch-on-focus).
 *
 * `cache: "no-store"` inside the fetchers + `dynamic = "force-dynamic"`
 * here keep this page off the Vercel ISR cache - history is live data,
 * stale renders mislead the demo.
 */
import { Shell } from "@/components/shell";
import {
  fetchCycles,
  fetchPortfolio,
  type CycleSummary,
  type Portfolio,
} from "@/lib/agent-api";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  // Best-effort: if the agent API is down (cold dev environment with
  // no DATABASE_URL yet, or the agent on Hetzner is offline) we still
  // want the page to render with empty initial data. The Shell's
  // panels handle the empty/error states themselves.
  let initialCycles: CycleSummary[] = [];
  let initialPortfolio: Portfolio | null = null;
  try {
    initialCycles = await fetchCycles({ limit: 50 });
  } catch {
    // Swallow - Shell tabs will fall back to client-side fetch +
    // error banners.
  }
  try {
    initialPortfolio = await fetchPortfolio();
  } catch {
    // Same - pass null through.
  }
  return (
    <Shell
      initialCycles={initialCycles}
      initialPortfolio={initialPortfolio}
    />
  );
}
