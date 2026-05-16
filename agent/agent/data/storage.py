from pathlib import Path
from typing import Literal
import pandas as pd

DATA_ROOT = Path(__file__).resolve().parents[3] / "data"


def save_parquet(df: pd.DataFrame, name: str, kind: Literal["raw", "processed", "live"]) -> Path:
    out_dir = DATA_ROOT / kind
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.parquet"
    df.to_parquet(path, engine="pyarrow", index=False)
    return path


def load_parquet(name: str, kind: Literal["raw", "processed", "live"]) -> pd.DataFrame:
    return pd.read_parquet(DATA_ROOT / kind / f"{name}.parquet", engine="pyarrow")
