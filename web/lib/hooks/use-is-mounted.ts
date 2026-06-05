"use client";

import { useEffect, useState } from "react";

/**
 * Returns `false` during SSR + the very first client render, then
 * `true` from the second render onward.
 *
 * Use as a hydration gate in any hook/component that reads
 * client-only state (wagmi data, `Date.now()`, `localStorage`,
 * `window.matchMedia`, …). The first render emits the same HTML as
 * the server (`mounted=false` branch); the subsequent re-render
 * unlocks the live data.
 */
export function useIsMounted(): boolean {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  return mounted;
}
