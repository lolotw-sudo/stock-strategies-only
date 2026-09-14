"""籌碼面判讀：誰在買、誰在賣，是洗盤還是出貨。

非《K線訊號判讀手冊》內容，只做參考、不參與燈號判定（看板 UI 須標明）。

核心是一張三維對照表：股價方向 × 主力方向 × 散戶方向。
  主力 = 外資 + 投信（自營商多為避險與造市，噪音大，只顯示不進判定）
  散戶 = 融資餘額增減（台股慣例的散戶代理指標，FinMind 沒有真正的散戶資料）
同方向沒有資訊量，背離才有：股價跌而法人買、融資退 → 籌碼從散戶換到法人手上（洗盤）；
股價漲而法人賣、融資增 → 籌碼從法人換到散戶手上（出貨）。

抓不到的：券商分點進出（哪家券商買賣）與八大官股銀行買賣，
FinMind 列為贊助會員資料集，免費 token 回 400。UNAVAILABLE 會誠實顯示在看板上。
"""
from __future__ import annotations

import pandas as pd

from . import datasources as ds

# ── 判定門檻 ──
MAIN_RATIO_TH = 0.01     # 主力 5 日淨買 ÷ 5 日成交量，±1% 以內視為沒方向
RETAIL_PCT_TH = 3.0      # 融資餘額 5 日變化，±3% 以內視為沒方向
PRICE_PCT_TH = 2.0       # 股價 5 日漲跌，±2% 以內視為盤整
STALE_DAYS = 5           # 籌碼資料日落後股價日超過這天數 → 標記過期

UNAVAILABLE = "券商分點進出（哪家券商在買）需 FinMind 贊助會員權限，免費金鑰抓不到"

LOTS = 1000              # FinMind 法人買賣單位是「股」，台股看的是「張」


def _dir(value: float, threshold: float) -> str:
    if value >= threshold:
        return "up"
    if value <= -threshold:
        return "down"
    return "flat"


# 判讀表：先看主力有沒有方向，再看 (股價, 主力, 散戶) 三者的搭配。
# 法人不動時股價方向沒有籌碼含義，所以主力 flat 只依散戶分三種，不再細分股價。
# price: up=漲 down=跌 flat=盤整；main: up=買超 down=賣超；retail: up=融資增 down=融資減
_MAIN_FLAT = {
    "up": ("retail_only", "融資獨撐",
           "法人這 5 日沒有明顯動作，融資餘額卻在增加——推動股價的是散戶資金，"
           "沒有法人買盤接手的話續航力有限。"),
    "down": ("quiet", "融資退場，法人觀望",
             "法人沒有明顯動作，融資餘額持續減少；浮額在清，但還看不到法人進場的跡象。"),
    "flat": ("neutral", "籌碼無明顯方向",
             "近 5 日法人與融資都沒有明確方向，籌碼面看不出誰在主導。"),
}

_READS = {
    # ── 法人買超 ──
    ("down", "up", "down"): (
        "washout", "疑似洗盤",
        "股價下跌，法人卻站買方，同時融資退場——籌碼正從散戶手上換到法人手上，"
        "這是洗盤最典型的組合。"),
    ("down", "up", "flat"): (
        "absorbing", "法人承接中",
        "股價下跌而法人買超，但融資沒什麼減少，籌碼換手還在進行、還沒洗乾淨。"),
    ("down", "up", "up"): (
        "absorbing", "法人承接，但融資沒退",
        "法人在跌勢中買超，融資餘額卻不減反增，散戶還沒被洗出去；"
        "籌碼沒清乾淨，跌勢通常還沒結束。"),
    ("flat", "up", "down"): (
        "washout", "盤整中法人吃貨",
        "股價橫盤，法人默默買超、融資默默退場——盤整區的籌碼正往法人手上集中。"),
    ("flat", "up", "flat"): (
        "accumulating", "法人默默進貨",
        "股價橫盤沒表態，法人已經在買超，融資則沒有動靜；籌碼安靜地往法人手上移動。"),
    ("flat", "up", "up"): (
        "accumulating", "法人與融資同時進場",
        "盤整區法人買超、融資也增加，兩邊都在卡位；誰先鬆手誰就是被倒貨的一方。"),
    ("up", "up", "down"): (
        "healthy", "法人推升，散戶沒跟上",
        "股價漲、法人買超、融資反而減少——上漲由法人買盤推動而不是散戶追價，"
        "這是最乾淨的上漲籌碼。"),
    ("up", "up", "flat"): (
        "healthy", "法人推升為主",
        "股價漲由法人買超帶動，融資沒有明顯跟進；買盤結構偏健康。"),
    ("up", "up", "up"): (
        "chase", "主力散戶同步追價",
        "法人與融資同步加碼，方向一致但也意味著追價情緒偏熱；"
        "一旦法人轉手，融資會是最先被倒貨的一方。"),
    # ── 法人賣超 ──
    ("down", "down", "down"): (
        "exodus", "多空同步退場",
        "股價跌、法人賣、融資也退，沒有人想留在場內；籌碼真空，"
        "跌完之後常需要一段時間才有新買盤。"),
    ("down", "down", "flat"): (
        "selling", "法人調節，融資沒動",
        "法人邊跌邊賣，融資餘額卻沒跟著減少；賣壓還沒宣洩完。"),
    ("down", "down", "up"): (
        "knife", "法人賣、散戶接",
        "法人邊跌邊賣，融資卻同步增加——散戶在接下跌刀，這是風險最高的籌碼組合。"),
    ("flat", "down", "down"): (
        "selling", "法人與融資同步減碼",
        "股價橫盤，法人賣超、融資也在退，兩邊都在撤；盤整區量能正在流失。"),
    ("flat", "down", "flat"): (
        "selling", "法人默默減碼",
        "股價橫盤沒表態，法人已經在賣超；表面平靜但籌碼在流出。"),
    ("flat", "down", "up"): (
        "distribution", "盤整中法人倒貨",
        "股價橫盤，法人賣超而融資增加——盤整區的籌碼正往散戶手上流。"),
    ("up", "down", "down"): (
        "weak_rally", "沒有主力的上漲",
        "股價漲，但法人是賣方、融資也在減，這段漲勢背後沒有法人買盤支撐。"),
    ("up", "down", "flat"): (
        "weak_rally", "沒有主力的上漲",
        "股價漲而法人賣超，漲勢缺少法人買盤支撐。"),
    ("up", "down", "up"): (
        "distribution", "疑似出貨",
        "股價上漲，法人卻在賣，融資同時增加——籌碼正從法人手上倒給散戶，"
        "這是出貨最典型的組合。"),
}

_FALLBACK = _MAIN_FLAT["flat"]


def _net_lots(series: pd.Series, n: int) -> float:
    """近 n 個交易日的淨買賣超，單位張（無條件捨去到整數張）。"""
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return 0.0
    return round(float(s.tail(n).sum()) / LOTS)


def analyze_chips(stock_id: str, px: pd.DataFrame, as_of: str | None = None) -> dict:
    """籌碼面板。px 是 data.get_price_history 的日線（需含 date/close/volume）。

    任何一段資料缺漏都不 raise：能算的照算，算不出來的欄位給 None，
    判讀退回 neutral 並在 notes 說明缺什麼。
    """
    if px is None or px.empty or "close" not in px.columns:
        return {"error": "缺少價格資料，籌碼無法對齊"}

    price_date = pd.Timestamp(px["date"].iloc[-1])
    start = (price_date - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    as_of = as_of or price_date.strftime("%Y-%m-%d")

    inst = ds.get_institutional(stock_id, start, as_of)
    margin = ds.get_margin(stock_id, start, as_of)
    holding = ds.get_shareholding(stock_id, start, as_of)

    notes: list[str] = []
    out: dict = {
        "price_date": price_date.strftime("%Y-%m-%d"),
        "unavailable": UNAVAILABLE,
    }

    # ── 三大法人：1 / 5 / 20 日淨買賣超（張）──
    flows = []
    if inst.empty:
        notes.append("三大法人買賣超無資料")
        out["date"] = None
        out["stale"] = False
    else:
        chip_date = pd.Timestamp(inst["date"].iloc[-1])
        out["date"] = chip_date.strftime("%Y-%m-%d")
        lag = len(px[px["date"] > chip_date])
        out["stale"] = lag > STALE_DAYS
        out["lag_days"] = lag
        for key, label, col in (
            ("foreign", "外資", "foreign_net"),
            ("trust", "投信", "trust_net"),
            ("dealer", "自營商", "dealer_net"),
            ("total", "三大法人合計", "total_net"),
        ):
            if col not in inst.columns:
                continue
            flows.append({
                "key": key, "label": label,
                "d1": _net_lots(inst[col], 1),
                "d5": _net_lots(inst[col], 5),
                "d20": _net_lots(inst[col], 20),
            })
    out["flows"] = flows

    # ── 散戶代理：融資餘額 ──
    retail = None
    if margin.empty or "margin_balance" not in margin.columns:
        notes.append("融資融券無資料")
    else:
        mb = pd.to_numeric(margin["margin_balance"], errors="coerce").dropna()
        if len(mb) >= 6:
            now, base5 = float(mb.iloc[-1]), float(mb.iloc[-6])
            base20 = float(mb.iloc[-21]) if len(mb) >= 21 else None
            ratio = margin["short_margin_ratio"].iloc[-1] if "short_margin_ratio" in margin else None
            retail = {
                "margin_balance": round(now),
                "chg_5d": round(now - base5),
                "chg_5d_pct": round((now / base5 - 1) * 100, 2) if base5 else None,
                "chg_20d_pct": round((now / base20 - 1) * 100, 2) if base20 else None,
                "short_margin_ratio": (round(float(ratio) * 100, 2)
                                       if ratio is not None and pd.notna(ratio) else None),
            }
        else:
            notes.append("融資餘額樣本不足 6 日")
    out["retail"] = retail

    # ── 外資持股比率（週頻，看的是趨勢不是當日）──
    fh = None
    if not holding.empty and "foreign_ratio" in holding.columns:
        fr = pd.to_numeric(holding["foreign_ratio"], errors="coerce").dropna()
        if not fr.empty:
            fh = {
                "now": round(float(fr.iloc[-1]), 2),
                "chg_20": (round(float(fr.iloc[-1] - fr.iloc[-21]), 2) if len(fr) >= 21 else None),
            }
    out["foreign_holding"] = fh

    # ── 三維判讀 ──
    close = pd.to_numeric(px["close"], errors="coerce").dropna()
    price_5d_pct = (round((float(close.iloc[-1]) / float(close.iloc[-6]) - 1) * 100, 2)
                    if len(close) >= 6 else None)

    main_lots = main_ratio = None
    if flows and "volume" in px.columns:
        main_lots = sum(f["d5"] for f in flows if f["key"] in ("foreign", "trust"))
        vol5 = float(pd.to_numeric(px["volume"], errors="coerce").tail(5).sum()) / LOTS
        main_ratio = round(main_lots / vol5 * 100, 3) if vol5 else None

    if price_5d_pct is None or main_ratio is None or retail is None:
        code, label, headline = _FALLBACK
        notes.append("資料不足以判讀洗盤／出貨，只列原始籌碼")
    else:
        p_dir = _dir(price_5d_pct, PRICE_PCT_TH)
        m_dir = _dir(main_ratio, MAIN_RATIO_TH * 100)
        r_dir = _dir(retail["chg_5d_pct"] or 0.0, RETAIL_PCT_TH)
        if m_dir == "flat":
            code, label, headline = _MAIN_FLAT[r_dir]
        else:
            code, label, headline = _READS[(p_dir, m_dir, r_dir)]

    out["read"] = {
        "code": code,
        "label": label,
        "headline": headline,
        "price_5d_pct": price_5d_pct,
        "main_5d_lots": main_lots,
        "main_5d_ratio": main_ratio,
        "retail_5d_pct": retail["chg_5d_pct"] if retail else None,
    }
    out["notes"] = notes
    return out
