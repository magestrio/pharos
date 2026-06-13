// Windowed `eth_getLogs`.
//
// The public Mantle RPC caps a single `eth_getLogs` call at a 10_000-block
// range — a deploy..latest scan (the vUSDC contracts sit ~250k blocks behind
// head) is rejected with "block range greater than 10000 max". viem does NOT
// auto-chunk, so the query throws and whatever reads it (Decision Log,
// attestor push count, reputation history) renders EMPTY even though the chain
// holds every event. Walk the range in <=10k windows instead.
//
// Generic over the fetch callback so each hook keeps viem's typed log return —
// this only owns the range math + bounded concurrency, not the contract shape.

export const MAX_LOG_RANGE = 9_000n;
const LOG_CHUNK_CONCURRENCY = 4;

export async function collectLogsInRanges<T>(
  fromBlock: bigint,
  toBlock: bigint,
  fetchRange: (from: bigint, to: bigint) => Promise<readonly T[]>,
): Promise<T[]> {
  const ranges: Array<{ from: bigint; to: bigint }> = [];
  for (let from = fromBlock; from <= toBlock; from += MAX_LOG_RANGE + 1n) {
    const to = from + MAX_LOG_RANGE > toBlock ? toBlock : from + MAX_LOG_RANGE;
    ranges.push({ from, to });
  }
  const out: T[] = [];
  for (let i = 0; i < ranges.length; i += LOG_CHUNK_CONCURRENCY) {
    const batch = await Promise.all(
      ranges.slice(i, i + LOG_CHUNK_CONCURRENCY).map((r) => fetchRange(r.from, r.to)),
    );
    for (const logs of batch) out.push(...logs);
  }
  return out;
}
