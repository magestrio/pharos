import json
from pathlib import Path
from typing import Any

# Foundry compiles per-contract JSON into `contracts/out/<Contract>.sol/<Contract>.json`.
# This path is relative to the repo root; the process must be launched from there
# or the env can override via OracleSettings if we ever need flexibility.
_DEFAULT_ARTIFACT = Path("contracts/out/BybitAttestor.sol/BybitAttestor.json")


def load_bybit_attestor_abi(artifact_path: Path | None = None) -> list[dict[str, Any]]:
    path = artifact_path or _DEFAULT_ARTIFACT
    with path.open("r") as f:
        artifact = json.load(f)
    return artifact["abi"]
