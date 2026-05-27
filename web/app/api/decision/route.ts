import { NextResponse } from "next/server";

import { readLatestDecision } from "@/lib/snapshot";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  const decision = await readLatestDecision();
  if (!decision) {
    return NextResponse.json({ error: "no decision available" }, { status: 404 });
  }
  return NextResponse.json(decision);
}
