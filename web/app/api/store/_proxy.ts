/**
 * Shared helper for the `/api/store/*` proxy routes
 * (`frontend-complete.5`).
 *
 * The browser hits these same-origin Next.js routes; this module
 * forwards the request to the agent FastAPI on Hetzner. Keeps
 * Postgres bound to localhost (no need to expose pg port to Vercel),
 * keeps CORS out of the picture, and gives one central place to add
 * caching / auth later.
 */
import { NextResponse } from "next/server";

const BASE = (process.env.AGENT_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

/**
 * Forward GET → upstream FastAPI. Preserves query string verbatim.
 * Returns:
 *  - 200 with upstream body on success
 *  - 404 / 503 verbatim from upstream
 *  - 502 if upstream is unreachable (network error) - surfaces a
 *    distinct status code so the client knows it's an infra issue,
 *    not a missing resource.
 */
export async function proxyGet(
  request: Request,
  upstreamPath: string,
): Promise<Response> {
  const url = new URL(request.url);
  const upstream = `${BASE}${upstreamPath}${url.search}`;
  try {
    const res = await fetch(upstream, {
      cache: "no-store",
      headers: { accept: "application/json" },
    });
    const body = await res.text();
    return new NextResponse(body, {
      status: res.status,
      headers: { "content-type": "application/json" },
    });
  } catch (e) {
    const message = e instanceof Error ? e.message : "unreachable";
    return NextResponse.json(
      { detail: `upstream unreachable: ${message}`, upstream },
      { status: 502 },
    );
  }
}
