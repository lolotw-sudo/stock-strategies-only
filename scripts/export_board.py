"""每日選股看板資料匯出（供 GitHub Pages 靜態看板使用）

讀 watchlist → 對每個策略評分（套大盤/夜盤濾鏡）→ 對每檔股票另外跑一次
朱家泓K線深度分析（與策略無關，全域只存一份，由各策略結果用 stock_id 參照）
→ 寫出 site/data/latest.json。

執行: uv run python scripts/export_board.py
環境變數：
    BOARD_STRATEGIES  逗號分隔的策略 id 清單，預設 "kline-chu,default"
    （其餘環境變數同 main.py，但 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 非必要）
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from stock_strategies.data import get_price_history
from stock_strategies.evaluate import evaluate
from stock_strategies.json_safe import json_safe
from stock_strategies.kline_report import analyze as analyze_kline
from stock_strategies.loader import get_strategy
from stock_strategies.market import apply_market_filter, get_market_state
from stock_strategies.night_session import (
    apply_night_filter,
    get_night_session,
    night_filter_note,
)
from stock_strategies.sheet import read_watchlist

# Telegram 兩個變數對看板匯出非必要（main.py 才需要）
REQUIRED_ENV = ["FINMIND_TOKEN", "GOOGLE_SHEET_ID", "GOOGLE_CREDS_JSON"]

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "site" / "data" / "latest.json"

ORDER = {"BUY": 0, "WATCH": 1, "SKIP": 2, "ERROR": 3}


def main():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"❌ 缺少環境變數: {missing}", file=sys.stderr)
        sys.exit(1)

    strategy_ids = [
        s.strip()
        for s in os.environ.get("BOARD_STRATEGIES", "kline-chu,default").split(",")
        if s.strip()
    ]

    print(f"[{datetime.now()}] 讀取 watchlist...")
    watchlist = read_watchlist()
    print(f"  → {len(watchlist)} 檔啟用中")

    print("取得大盤狀態...")
    market = get_market_state()
    print(f"  → {market['note']}")

    print("取得昨晚夜盤...")
    try:
        night = get_night_session()
    except Exception as e:
        print(f"  → 夜盤資料取得失敗: {str(e)[:80]}")
        night = None
    night_note = night_filter_note(night)
    print(f"  → {night_note}")

    strategies = []
    for sid_strategy in strategy_ids:
        strategy = get_strategy(sid_strategy)
        if not strategy:
            print(f"⚠️ 找不到策略 {sid_strategy}，略過")
            continue
        strategies.append(strategy)

    if not strategies:
        print("❌ BOARD_STRATEGIES 內沒有任何有效策略", file=sys.stderr)
        sys.exit(1)

    # results_by_id[strategy_id] = list[dict]
    results_by_id: dict[str, list] = {s["id"]: [] for s in strategies}
    analysis: dict = {}

    for i, row in enumerate(watchlist, 1):
        sid = str(row["stock_id"])
        name = row.get("name", "")
        print(f"[{i}/{len(watchlist)}] {sid} {name}")

        for strategy in strategies:
            try:
                r = evaluate(sid, name, strategy)
            except Exception as e:
                r = {
                    "stock_id": sid,
                    "name": name,
                    "strategy_id": strategy["id"],
                    "action": "ERROR",
                    "risk_notes": [f"錯誤: {str(e)[:80]}"],
                }
            if r:
                results_by_id[strategy["id"]].append(r)

        # K線深度分析與策略無關，同一檔股票整份 JSON 只跑一次、只存一份
        try:
            px = get_price_history(sid, 1)
            if px.empty:
                analysis[sid] = {"error": "找不到價格資料"}
            else:
                analysis[sid] = analyze_kline(px, sid, name)
        except Exception as e:
            analysis[sid] = {"error": str(e)[:200]}

        # 節流：同一檔股票的所有策略評分與K線分析共用快取，只需在每檔股票之間節流一次
        time.sleep(0.6)

    strategies_out = []
    for strategy in strategies:
        results = results_by_id[strategy["id"]]

        downgraded = apply_market_filter(results, market)
        if downgraded:
            print(f"⚠️ [{strategy['id']}] 大盤跌破月線，{downgraded} 檔 BUY 已自動降為 WATCH")

        night_downgraded = apply_night_filter(results, night)
        if night_downgraded:
            print(f"🌙 [{strategy['id']}] 昨晚夜盤大跌，{night_downgraded} 檔 BUY 已自動降為 WATCH")

        results.sort(key=lambda x: (ORDER.get(x.get("action"), 4), -x.get("signal_score", 0)))

        summary = {
            "total": len(results),
            "buy": sum(1 for r in results if r.get("action") == "BUY"),
            "watch": sum(1 for r in results if r.get("action") == "WATCH"),
            "skip": sum(1 for r in results if r.get("action") == "SKIP"),
            "error": sum(1 for r in results if r.get("action") == "ERROR"),
        }
        print(
            f"  → [{strategy['id']}] BUY {summary['buy']} / WATCH {summary['watch']} / "
            f"SKIP {summary['skip']} / ERROR {summary['error']}"
        )

        strategies_out.append({
            "id": strategy["id"],
            "name": strategy["name"],
            "description": strategy.get("description", ""),
            "summary": summary,
            "results": results,
        })

    now = datetime.now(ZoneInfo("Asia/Taipei"))
    payload = {
        "generated_at": now.isoformat(),
        "generated_at_display": now.strftime("%Y-%m-%d %H:%M（台北時間）"),
        "market": {
            "bullish": market.get("bullish"),
            "close": market.get("close"),
            "ma20": market.get("ma20"),
            "note": market.get("note"),
        },
        "night_note": night_note,
        "watchlist_count": len(watchlist),
        "strategies": strategies_out,
        "analysis": analysis,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(json_safe(payload), f, ensure_ascii=False, indent=2)

    print(f"✅ 完成，已寫入 {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
