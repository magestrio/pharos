import { proxyGet } from "../../_proxy";

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  { params }: { params: { cycle_ts: string } },
) {
  return proxyGet(
    request,
    `/cycles/${encodeURIComponent(params.cycle_ts)}`,
  );
}
