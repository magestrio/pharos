from datetime import datetime, timezone

import pandas as pd

from agent.data.storage import save_parquet


async def fetch_allora_schema() -> None:
    timestamps = pd.date_range(
        end=datetime.now(tz=timezone.utc),
        periods=90 * 6,
        freq="4h",
        tz="UTC",
    )
    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "topic_id": "eth_price_7d",
            "network_inference": float("nan"),
            "confidence_low": float("nan"),
            "confidence_high": float("nan"),
            "is_synthetic": True,
        }
    )
    save_parquet(df, "allora_synthetic", "raw")
    print("[allora] backtest uses synthetic schema (NaN), live collector runs separately")
