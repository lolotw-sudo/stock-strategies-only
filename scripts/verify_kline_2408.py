"""驗證腳本：用南亞科(2408)本機快取資料跑 detect_kline，
對照《K線訊號判讀手冊》7-6 案例（2026/07/29 ~ 08/14）逐日輸出。

用法：
    .venv/bin/python scripts/verify_kline_2408.py
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock_strategies.kline import detect_kline

CACHE_FILE = (
    Path(__file__).resolve().parent.parent
    / ".cache"
    / "finmind"
    / "TaiwanStockPrice__2408.parquet"
)

START_DATE = "2026-07-29"
END_DATE = "2026-08-14"


def main() -> None:
    df = pd.read_parquet(CACHE_FILE)
    df = df.rename(columns={"max": "high", "min": "low", "Trading_Volume": "volume"})
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)

    mask = (df["date"] >= START_DATE) & (df["date"] <= END_DATE)
    target_dates = df.loc[mask, "date"]

    header = (
        f"{'日期':<12}{'收盤':>8}  {'位置':<6}{'第幾天':>6}  "
        f"{'領域':<8}{'買%':>6}{'賣%':>6}  {'訊號':<24}{'警訊':<40}{'bonus':>6}"
    )
    print(header)
    print("-" * len(header))

    for idx in target_dates.index:
        r = detect_kline(df, idx)
        date_str = df.loc[idx, "date"].strftime("%Y-%m-%d")
        close = df.loc[idx, "close"]
        domain = r["domain"]
        print(
            f"{date_str:<12}{close:>8.1f}  {r['position']:<6}{r['leg_days']:>6}  "
            f"{domain['type']:<8}{domain['buyer_pct']:>6.1f}{domain['seller_pct']:>6.1f}  "
            f"{'、'.join(r['signals']) or '—':<24}"
            f"{'；'.join(r['warnings']) or '—':<40}{r['bonus']:>6}"
        )


if __name__ == "__main__":
    main()
