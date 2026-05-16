import json
from pathlib import Path
from typing import Any

_MEMORY_FILE = Path(__file__).parent.parent / "memory.json"


def load() -> list[dict[str, Any]]:
    if not _MEMORY_FILE.exists():
        return []
    return json.loads(_MEMORY_FILE.read_text())


def append(entry: dict[str, Any]) -> None:
    history = load()
    history.append(entry)
    _MEMORY_FILE.write_text(json.dumps(history, indent=2))
