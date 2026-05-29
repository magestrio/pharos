import { proxyGet } from "../_proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return proxyGet(request, "/cycles");
}
