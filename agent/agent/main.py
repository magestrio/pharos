"""Agent entrypoint.

The pre-pivot mainnet daemon (`agent.loop` driven by `agent.scheduler`)
was retired in `bybit-sandbox.30` along with the rest of the legacy
stack (gather / execute / reason.client). The current production path
is the Bybit sandbox loop:

    python -m agent.sandbox.snapshot                   # write one snapshot
    python -m agent.sandbox.decide --snapshot <path>   # decide + validate

A long-running daemon will land alongside `bybit-sandbox.11` (sandbox
executor) and the mainnet-bridge work. Until then this entrypoint is a
stub that runs one decide cycle against the freshest snapshot — useful
for local smoke testing, not intended as production orchestration.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from agent.sandbox.snapshot import SNAPSHOT_DIR

    snapshots = sorted(Path(SNAPSHOT_DIR).glob("*.json"))
    if not snapshots:
        print(
            "no snapshot yet — run `python -m agent.sandbox.snapshot` first",
            file=sys.stderr,
        )
        raise SystemExit(2)

    from agent.sandbox.decide import _main as decide_main

    sys.argv = ["agent.sandbox.decide", "--snapshot", str(snapshots[-1])]
    decide_main()


if __name__ == "__main__":
    main()
