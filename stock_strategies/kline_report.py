"""朱家泓K線深度分析報告模組（V2）

依據《K線訊號判讀手冊》：
- 3-1　波浪型態
- 5-2-0　搶反彈四問
- 5-2-2　空頭轉多頭四階段（不可跳關）
- 第六章　判讀矩陣
- 7-3　創新高四關（乖離、天數）
- 7-5　停利五法
- 第八章　7步SOP ＋ 停損五法

鐵律：所有計算只使用 idx 當天（含）以前的資料，不使用未來函數。
呼叫方式：analyze(df, stock_id, name, idx=-1)，df 需含小寫 open/high/low/close/volume/date。
"""

from __future__ import annotations

import pandas as pd

from .kline import detect_kline

MIN_ROWS = 60

# ── 3-1　波浪型態：fractal pivot（左右各 PIVOT_K 日內最大/最小才算確認轉折點）──
PIVOT_K = 3
WAVE_LOOKBACK = 120       # 只在最近120個交易日內找轉折點
WAVE_RECENT_N = 3         # 保留最近幾個轉折高/低

# ── 5-2-2　空頭轉多頭四階段 ──
BOTTOM_NEWLOW_WINDOW = 20     # 「創20日新低」視窗
BOTTOM_STAGE1_DAYS = 10       # 近10日未創新低才算①止跌
MA_SLOPE_LOOKBACK = 5         # 均線斜率近似：今日 vs 5日前
STAGE3_BREAKOUT_VOL_MULT = 1.5  # ③突破日量 ≥ 前5日均量1.5倍

# ── 5-2-0　搶反彈四問 ──
REBOUND_WINDOW = 20
REBOUND_DRAWDOWN_PCT = 0.15      # ①急跌：近20日內最大回檔 ≥15%
# ②爆量：滿足任一即通過（手冊7-6南亞科案例 7/30 量120,154 vs 前一日56,513=2.13x，
#   對前5日均量僅1.63x未達門檻(a)，但符合本專案 volume.py「倍量柱」門檻(b)，故納入）
REBOUND_VOL_MULT_5DAY = 2.0      # (a) 量 ≥ 前5日均量2倍
REBOUND_VOL_MULT_PREV_DAY = 2.0  # (b) 量 ≥ 前一日2倍，同 stock_strategies/volume.py「倍量柱」定義
REBOUND_SIGNAL_WINDOW = 10       # ③止跌：近10日內找多方訊號
REBOUND_BULLISH_SIGNALS = {"錘子", "多方吞噬", "貫穿線", "晨星", "長下影"}
LONG_LOWER_SHADOW_MULT = 2.0     # 長下影線：下影 ≥ 2x 實體

# ── 第八章 SOP：均線糾結 ──
MA_TANGLE_PCT = 0.02   # 三線(5/10/20)最大最小差 <2%（相對最小值）視為糾結

# ── 紀律 ──
BIAS_MA20_HOT_PCT = 0.20   # 對MA20乖離 >20% 視為過熱
HIGH_LEG_DAYS_MIN = 7      # 高檔且走了≥7天才觸發「高檔不追多」不過

# ── 停損五法 ──
FIXED_STOP_PCT = 0.05


# ────────────────────────── 波浪型態 ──────────────────────────


def _find_pivots(hist: pd.DataFrame, li: int) -> tuple[list, list]:
    """回傳 (highs, lows)，每項為 (pos, date, price)，只含已確認（pos+PIVOT_K<=li）
    且在 WAVE_LOOKBACK 視窗內的轉折點。"""
    highs, lows = [], []
    start = max(PIVOT_K, li - WAVE_LOOKBACK + 1)
    end = li - PIVOT_K + 1
    for j in range(start, end):
        lo, hi = j - PIVOT_K, j + PIVOT_K
        if lo < 0:
            continue
        seg_h = hist["high"].iloc[lo: hi + 1]
        seg_l = hist["low"].iloc[lo: hi + 1]
        if float(hist["high"].iloc[j]) == float(seg_h.max()):
            highs.append((j, hist["date"].iloc[j], float(hist["high"].iloc[j])))
        if float(hist["low"].iloc[j]) == float(seg_l.min()):
            lows.append((j, hist["date"].iloc[j], float(hist["low"].iloc[j])))
    return highs, lows


def _dist_label(distance_pct: float) -> str:
    """distance_pct=(現價-停損價)/現價*100：正常為正值（停損在現價下方）。
    若該停損法算出的價位已在現價之上（例如收盤已跌破該均線），值會是負的，
    代表用該方法「已經跌破」，不能再套用「-」前綴（會變成 --0.4% 的顯示錯誤）。"""
    if distance_pct >= 0:
        return f"-{distance_pct:.1f}%"
    return f"已跌破（+{abs(distance_pct):.1f}%）"


def _fmt_pivot(p: tuple) -> dict:
    return {"date": pd.Timestamp(p[1]).strftime("%Y-%m-%d"), "price": p[2]}


def _wave_analysis(highs: list, lows: list, close_today: float) -> dict:
    """依 3-1 波浪型態判定 trend/pattern，並依「多空易位的判定」納入當前收盤與
    最近一個已確認轉折高/低的比較（無未來函數：只用已確認 pivot + 當日收盤）。"""
    recent_highs = highs[-WAVE_RECENT_N:]
    recent_lows = lows[-WAVE_RECENT_N:]
    enough = len(recent_highs) >= 2 and len(recent_lows) >= 2

    higher_high = len(recent_highs) >= 2 and recent_highs[-1][2] > recent_highs[-2][2]
    higher_low = len(recent_lows) >= 2 and recent_lows[-1][2] > recent_lows[-2][2]

    last_high = recent_highs[-1] if recent_highs else None
    last_low = recent_lows[-1] if recent_lows else None
    broke_above_last_high = last_high is not None and close_today > last_high[2]
    broke_below_last_low = last_low is not None and close_today < last_low[2]

    break_notes = []
    if last_high is not None:
        pct = (close_today / last_high[2] - 1) * 100
        break_notes.append(
            f"今日收盤{close_today:.1f}{'已突破' if broke_above_last_high else '尚未突破'}"
            f"最近確認轉折高{last_high[2]:.1f}（{pct:+.1f}%）"
        )
    if last_low is not None:
        pct_l = (close_today / last_low[2] - 1) * 100
        break_notes.append(
            f"{'已跌破' if broke_below_last_low else '尚未跌破'}"
            f"最近確認轉折低{last_low[2]:.1f}（{pct_l:+.1f}%）"
        )
    break_suffix = ("；" + "；".join(break_notes)) if break_notes else ""

    if not enough:
        trend = "盤整"
        pattern = "轉折點不足，無法判斷型態"
        evidence = (
            f"近{WAVE_LOOKBACK}日內確認的轉折高{len(highs)}個、"
            f"轉折低{len(lows)}個，不足以判斷型態" + break_suffix
        )
    else:
        if higher_high and higher_low:
            trend = "多頭"
        elif not higher_high and not higher_low:
            trend = "打底突破中" if broke_above_last_high else "空頭"
        else:
            trend = "盤整"

        if trend == "打底突破中":
            pattern = "頭頭低、底底低，但已突破前高（多空易位進行中）"
        else:
            hh_word = "頭頭高" if higher_high else "頭頭低"
            hl_word = "底底高" if higher_low else "底底低"
            pattern = f"{hh_word}、{hl_word}"

        if trend == "盤整":
            if broke_above_last_high:
                trend = "盤整偏多"
            elif broke_below_last_low:
                trend = "盤整偏空"

        if trend == "多頭" and broke_below_last_low:
            trend = "多頭轉弱"

        h2, h1 = recent_highs[-2], recent_highs[-1]
        l2, l1 = recent_lows[-2], recent_lows[-1]
        evidence = (
            f"轉折高：{pd.Timestamp(h2[1]).strftime('%m/%d')} {h2[2]:.1f} → "
            f"{pd.Timestamp(h1[1]).strftime('%m/%d')} {h1[2]:.1f}"
            f"（{'較高' if higher_high else '較低'}）；"
            f"轉折低：{pd.Timestamp(l2[1]).strftime('%m/%d')} {l2[2]:.1f} → "
            f"{pd.Timestamp(l1[1]).strftime('%m/%d')} {l1[2]:.1f}"
            f"（{'較高' if higher_low else '較低'}）" + break_suffix
        )

    return {
        "trend": trend,
        "pattern": pattern,
        "higher_low": bool(higher_low),
        "higher_high": bool(higher_high),
        "broke_above_last_high": bool(broke_above_last_high),
        "broke_below_last_low": bool(broke_below_last_low),
        "recent_highs": [_fmt_pivot(p) for p in recent_highs],
        "recent_lows": [_fmt_pivot(p) for p in recent_lows],
        "evidence": evidence,
    }


# ────────────────────────── 5-2-2　打底四階段 ──────────────────────────


def _has_long_lower_shadow(hist: pd.DataFrame, d: int) -> bool:
    o = float(hist["open"].iloc[d])
    h = float(hist["high"].iloc[d])
    l = float(hist["low"].iloc[d])
    c = float(hist["close"].iloc[d])
    rng = h - l
    if rng <= 0:
        return False
    body = abs(c - o)
    lower_shadow = min(o, c) - l
    if body > 0:
        return lower_shadow >= LONG_LOWER_SHADOW_MULT * body
    return lower_shadow / rng >= 0.6


def _bottom_stage(
    hist: pd.DataFrame, li: int, wave: dict, ma5: pd.Series, ma10: pd.Series, ma20: pd.Series
) -> dict:
    checks = []

    # ①止跌：近10日未創20日新低
    newlow_days = []
    for d in range(max(0, li - BOTTOM_STAGE1_DAYS + 1), li + 1):
        w_start = max(0, d - BOTTOM_NEWLOW_WINDOW + 1)
        window_low = float(hist["low"].iloc[w_start: d + 1].min())
        if float(hist["low"].iloc[d]) <= window_low:
            newlow_days.append(pd.Timestamp(hist["date"].iloc[d]).strftime("%m/%d"))
    stage1_pass = len(newlow_days) == 0
    stage1_detail = (
        f"已止跌：近{BOTTOM_STAGE1_DAYS}日未創{BOTTOM_NEWLOW_WINDOW}日新低。"
        if stage1_pass
        else f"尚未止跌：近{BOTTOM_STAGE1_DAYS}日內仍創新低：{'、'.join(newlow_days)}。"
    )
    checks.append({"name": "①止跌", "pass": stage1_pass, "detail": stage1_detail})

    # ②打底：底底高成立 且 MA10/MA20 由下彎轉走平／翻揚（近似：今日≥5日前）
    ma10_today, ma20_today = float(ma10.iloc[li]), float(ma20.iloc[li])
    if li >= MA_SLOPE_LOOKBACK and pd.notna(ma10.iloc[li - MA_SLOPE_LOOKBACK]) and pd.notna(ma10_today):
        ma10_prior = float(ma10.iloc[li - MA_SLOPE_LOOKBACK])
        ma20_prior = float(ma20.iloc[li - MA_SLOPE_LOOKBACK])
        ma_turning = ma10_today >= ma10_prior and ma20_today >= ma20_prior
        higher_low = bool(wave["higher_low"])
        recent_lows = wave.get("recent_lows") or []
        if len(recent_lows) >= 2:
            low_prev, low_last = recent_lows[-2]["price"], recent_lows[-1]["price"]
            if higher_low:
                low_rel = "高於"
            elif low_last == low_prev:
                low_rel = "持平於"
            else:
                low_rel = "仍低於"
            low_evidence = f"最近轉折低 {low_last:.1f} {low_rel}前低 {low_prev:.1f}"
        else:
            low_evidence = "底底高尚未成立" if not higher_low else "底底高已成立"
        ma_bare = f"MA10 {ma10_prior:.2f}→{ma10_today:.2f}、MA20 {ma20_prior:.2f}→{ma20_today:.2f}"
        ma_evidence = f"{ma_bare}（{'皆翻揚/走平' if ma_turning else '仍下彎'}）"
        if stage2_pass := (higher_low and ma_turning):
            stage2_detail = f"打底已完成：{low_evidence}，且{ma_evidence}。"
        elif not higher_low and not ma_turning:
            stage2_detail = f"底底高尚未成立：{low_evidence}；且{ma_evidence}。"
        elif not higher_low:
            stage2_detail = f"底底高尚未成立：{low_evidence}。（均線部分已達標：{ma_bare}，皆翻揚/走平）"
        else:
            stage2_detail = f"均線尚未翻揚：{ma_bare}，仍下彎。（底底高已成立）"
    else:
        ma_turning = False
        stage2_pass = False
        stage2_detail = "均線斜率資料不足：無法判斷MA10/MA20是否翻揚。"
    checks.append({"name": "②打底", "pass": stage2_pass, "detail": stage2_detail})

    # ③突破：收盤站上MA20 且 突破前一個轉折高 且 突破日量≥前5日均量1.5x
    close_today = float(hist["close"].iloc[li])
    above_ma20 = pd.notna(ma20_today) and close_today > ma20_today
    prior_high = wave["recent_highs"][-1]["price"] if wave["recent_highs"] else None
    breakout_high = prior_high is not None and close_today > prior_high
    vol5 = hist["volume"].iloc[max(0, li - 5): li]
    vol5ma = float(vol5.mean()) if len(vol5) > 0 else 0.0
    vol_today = float(hist["volume"].iloc[li])
    breakout_vol = vol5ma > 0 and vol_today >= STAGE3_BREAKOUT_VOL_MULT * vol5ma
    stage3_pass = bool(above_ma20 and breakout_high and breakout_vol)
    vol_ratio = vol_today / vol5ma if vol5ma > 0 else 0.0
    prior_high_str = f"{prior_high:.1f}" if prior_high is not None else "N/A"
    price_ok = above_ma20 and breakout_high
    price_evidence = f"收盤 {close_today:.1f} 站上 MA20 {ma20_today:.1f}、且突破前轉折高 {prior_high_str}"
    vol_evidence = f"突破量為前5日均量 {vol_ratio:.2f}x"
    if stage3_pass:
        stage3_detail = f"已突破：{price_evidence}，{vol_evidence}，達{STAGE3_BREAKOUT_VOL_MULT:.1f}x門檻。"
    elif not price_ok:
        price_fail_parts = []
        if not above_ma20:
            price_fail_parts.append(f"收盤 {close_today:.1f} 未站上 MA20 {ma20_today:.1f}")
        if not breakout_high:
            price_fail_parts.append(f"尚未突破前轉折高 {prior_high_str}")
        stage3_detail = f"價格條件未達標：{'、'.join(price_fail_parts)}。"
        if breakout_vol:
            stage3_detail += f"（量能部分已達標：{vol_evidence}）"
    else:
        stage3_detail = (
            f"量能不足：{vol_evidence}，未達 {STAGE3_BREAKOUT_VOL_MULT:.1f}x 門檻。"
            f"（價格部分已達標：{price_evidence}）"
        )
    checks.append({"name": "③突破", "pass": stage3_pass, "detail": stage3_detail})

    # ④回升確立：MA5>MA10>MA20 且 trend=="多頭"
    ma5_today = float(ma5.iloc[li]) if pd.notna(ma5.iloc[li]) else None
    ma_aligned = ma5_today is not None and pd.notna(ma20_today) and ma5_today > ma10_today > ma20_today
    trend_bullish = wave["trend"] == "多頭"
    stage4_pass = bool(ma_aligned and trend_bullish)
    ma5_str = f"{ma5_today:.2f}" if ma5_today is not None else "N/A"
    ma_evidence4 = f"MA5={ma5_str} MA10={ma10_today:.2f} MA20={ma20_today:.2f}"
    if stage4_pass:
        stage4_detail = f"回升已確立：{ma_evidence4}多排，且趨勢已為多頭。"
    elif not ma_aligned and not trend_bullish:
        stage4_detail = f"均線未多排且趨勢尚未確立為多頭：{ma_evidence4}（未多排），目前為{wave['trend']}。"
    elif not trend_bullish:
        stage4_detail = f"趨勢尚未確立為多頭：目前為{wave['trend']}。（均線部分已達標：MA5>MA10>MA20 多排）"
    else:
        stage4_detail = f"均線尚未多排：{ma_evidence4}。（趨勢部分已達標：已為多頭）"
    checks.append({"name": "④回升確立", "pass": stage4_pass, "detail": stage4_detail})

    stage_index = 0
    for c in checks:
        if c["pass"]:
            stage_index += 1
        else:
            break
    stage_names = ["未止跌", "①止跌", "②打底", "③突破", "④回升確立"]

    # skipped_stages：自身條件已成立（check.pass 為 True），但因前面階段未過而未被
    # 連續計入 stage_index 的階段編號——依各 check 的 pass 布林判斷，與連續性無關。
    skipped_stages = [i + 1 for i, c in enumerate(checks) if c["pass"] and (i + 1) > stage_index]
    if skipped_stages:
        blocking = checks[stage_index]
        block_reason = blocking["detail"].split("：", 1)[0]
        skipped_names = "、".join(checks[s - 1]["name"] for s in skipped_stages)
        stage_note = (
            f"{blocking['name']}尚未通過（{block_reason}），但{skipped_names}的條件已成立"
            f"——手冊5-2-2強調不可跳關，此為保守判定。"
        )
    else:
        stage_note = ""

    return {
        "stage": stage_names[stage_index],
        "stage_index": stage_index,
        "checks": checks,
        "skipped_stages": skipped_stages,
        "stage_note": stage_note,
    }


# ────────────────────────── 5-2-0　搶反彈四問 ──────────────────────────


def _rebound_check(hist: pd.DataFrame, li: int, wave: dict, highs: list) -> dict:
    win_start = max(0, li - REBOUND_WINDOW + 1)
    window = hist.iloc[win_start: li + 1]

    # ①急跌：近20日內最大回檔 ≥15%（自區間內任一高點到其後低點）
    running_max = window["high"].cummax()
    drawdown = (running_max - window["low"]) / running_max
    max_dd = float(drawdown.max())
    dd_pos = int(drawdown.values.argmax())
    peak_val = float(running_max.iloc[dd_pos])
    trough_val = float(window["low"].iloc[dd_pos])
    q1_pass = max_dd >= REBOUND_DRAWDOWN_PCT
    q1_detail = f"近{REBOUND_WINDOW}日最大回檔{max_dd*100:.1f}%（{peak_val:.1f}→{trough_val:.1f}）"

    # ②爆量：任一成立即通過 (a) 量≥前5日均量2倍 或 (b) 量≥前一日2倍（倍量柱）
    q2_pass = False
    hit_kind = None
    hit_date = None
    hit_ratio = 0.0
    best_5day_ratio, best_5day_date = 0.0, None
    for d in range(win_start, li + 1):
        prior5 = hist["volume"].iloc[max(0, d - 5): d]
        m5 = float(prior5.mean()) if len(prior5) > 0 else 0.0
        v = float(hist["volume"].iloc[d])
        ratio5 = v / m5 if m5 > 0 else 0.0
        if ratio5 > best_5day_ratio:
            best_5day_ratio, best_5day_date = ratio5, hist["date"].iloc[d]
        pass_5day = m5 > 0 and v >= REBOUND_VOL_MULT_5DAY * m5

        prev_v = float(hist["volume"].iloc[d - 1]) if d >= 1 else 0.0
        ratio_prev = v / prev_v if prev_v > 0 else 0.0
        pass_prev_day = prev_v > 0 and v >= REBOUND_VOL_MULT_PREV_DAY * prev_v

        if not q2_pass and (pass_5day or pass_prev_day):
            q2_pass = True
            # 優先記錄倍量柱(b)命中（更貼近手冊案例判準），否則記錄(a)
            if pass_prev_day:
                hit_kind, hit_date, hit_ratio = "倍量柱(較前一日)", hist["date"].iloc[d], ratio_prev
            else:
                hit_kind, hit_date, hit_ratio = "較前5日均量", hist["date"].iloc[d], ratio5

    if q2_pass:
        q2_detail = (
            f"{pd.Timestamp(hit_date).strftime('%m/%d')}爆量：{hit_kind}{hit_ratio:.2f}x"
            f"（門檻(a)前5日均量{REBOUND_VOL_MULT_5DAY:.0f}x／(b)前一日{REBOUND_VOL_MULT_PREV_DAY:.0f}x，任一即通過）"
        )
    else:
        q2_detail = (
            f"近{REBOUND_WINDOW}日最大量能倍數{best_5day_ratio:.2f}x"
            f"（{pd.Timestamp(best_5day_date).strftime('%m/%d') if best_5day_date is not None else 'N/A'}，較前5日均量），"
            f"未達門檻(a){REBOUND_VOL_MULT_5DAY:.0f}x／(b){REBOUND_VOL_MULT_PREV_DAY:.0f}x（較前一日）"
        )

    # ③止跌：近10日內出現任一多方訊號（錘子/多方吞噬/貫穿線/晨星/長下影）或 higher_low 成立
    sig_start = max(0, li - REBOUND_SIGNAL_WINDOW + 1)
    hits = []
    for d in range(sig_start, li + 1):
        r = detect_kline(hist, d)
        found = REBOUND_BULLISH_SIGNALS.intersection(r["signals"])
        if _has_long_lower_shadow(hist, d):
            found = found | {"長下影"}
        if found:
            hits.append(f"{pd.Timestamp(hist['date'].iloc[d]).strftime('%m/%d')}{'/'.join(sorted(found))}")
    q3_pass = len(hits) > 0 or wave["higher_low"]
    q3_detail = (
        ("止跌訊號：" + "、".join(hits)) if hits else "近10日無明顯止跌K線"
    )
    if wave["higher_low"]:
        q3_detail += "；底底高成立"

    # ④過高：收盤突破近20日內某個已確認轉折高
    close_today = float(hist["close"].iloc[li])
    window_highs = [p for p in highs if p[0] >= win_start]
    broken = [p for p in window_highs if close_today > p[2]]
    q4_pass = len(broken) > 0
    if broken:
        ref = max(broken, key=lambda p: p[2])
        q4_detail = f"收盤{close_today:.2f}已突破{pd.Timestamp(ref[1]).strftime('%m/%d')}轉折高{ref[2]:.2f}"
    else:
        q4_detail = f"收盤{close_today:.2f}尚未突破近{REBOUND_WINDOW}日內任一轉折高"

    items = [
        {"q": "①有沒有急跌", "pass": q1_pass, "detail": q1_detail},
        {"q": "②有沒有爆量", "pass": q2_pass, "detail": q2_detail},
        {"q": "③有沒有止跌", "pass": q3_pass, "detail": q3_detail},
        {"q": "④有沒有過高", "pass": q4_pass, "detail": q4_detail},
    ]
    passed = sum(1 for it in items if it["pass"])
    if passed == 4:
        conclusion = "四問全過，屬有效搶反彈訊號"
    elif passed >= 2:
        conclusion = f"{passed}/4通過，訊號打折扣，需保守看待"
    else:
        conclusion = f"僅{passed}/4通過，不建議搶反彈"

    return {"passed": passed, "total": 4, "conclusion": conclusion, "items": items}


# ────────────────────────── 週線確認 ──────────────────────────


def _weekly_confirm(hist: pd.DataFrame, wave: dict) -> dict:
    h = hist.copy()
    h["date"] = pd.to_datetime(h["date"])
    weekly = h.set_index("date")["close"].resample("W").last().dropna()
    if len(weekly) < 5:
        return {"verdict": "資料不足", "detail": "週線資料不足5週"}
    weekly_ma5 = weekly.rolling(5).mean()
    w_close = float(weekly.iloc[-1])
    w_ma5 = weekly_ma5.iloc[-1]
    if pd.isna(w_ma5):
        return {"verdict": "資料不足", "detail": "週MA5尚無足夠資料"}
    w_ma5 = float(w_ma5)
    weekly_bullish = w_close > w_ma5
    trend = wave["trend"]
    BULLISH_TRENDS = {"多頭", "多頭轉弱", "打底突破中", "盤整偏多"}
    if trend == "盤整":
        verdict = "中性"
        detail = f"週收盤{w_close:.2f}{'站上' if weekly_bullish else '跌破'}週MA5({w_ma5:.2f})，日線趨勢{trend}，暫無明確方向可比對"
    else:
        daily_bullish = trend in BULLISH_TRENDS
        same_direction = weekly_bullish == daily_bullish
        # 同向即支持；不同向但週線偏多且日線非純空頭（如打底突破中），仍視為長線未否決，予以支持
        supportive = same_direction or (weekly_bullish and trend != "空頭")
        verdict = "支持" if supportive else "衝突"
        if same_direction:
            reason = "週線與日線方向一致"
        elif supportive:
            reason = "週線偏多，日線非純空頭，長線尚未否決"
        else:
            reason = "週線與日線方向衝突"
        detail = (
            f"週收盤{w_close:.2f}{'站上' if weekly_bullish else '跌破'}週MA5({w_ma5:.2f})，"
            f"日線趨勢{trend}，{reason}"
        )
    return {"verdict": verdict, "detail": detail}


# ────────────────────────── 停損五法 ──────────────────────────


def _stop_loss(hist: pd.DataFrame, li: int, snapshot: dict, ma5: pd.Series, ma10: pd.Series, lows: list) -> dict:
    close_today = float(hist["close"].iloc[li])
    prev_low = float(hist["low"].iloc[li - 1]) if li >= 1 else close_today
    ma5_v = float(ma5.iloc[li]) if pd.notna(ma5.iloc[li]) else close_today
    ma10_v = float(ma10.iloc[li]) if pd.notna(ma10.iloc[li]) else close_today
    confirmed_lows = [p for p in lows if p[0] < li]
    prior_wave_low = float(confirmed_lows[-1][2]) if confirmed_lows else close_today * (1 - FIXED_STOP_PCT)
    fixed = close_today * (1 - FIXED_STOP_PCT)

    def mk(method: str, price: float) -> dict:
        dist = (close_today - price) / close_today * 100 if close_today else 0.0
        return {"method": method, "price": round(price, 2), "distance_pct": round(dist, 2)}

    alternatives = [
        mk("前一日低點法", prev_low),
        mk("MA5法", ma5_v),
        mk("MA10法", ma10_v),
        mk("前波低點法", prior_wave_low),
        mk("固定5%法", fixed),
    ]
    idx_by_method = {a["method"]: a for a in alternatives}

    position = snapshot.get("position", "")
    if position == "高檔":
        rec = idx_by_method["MA5法"]
        reason = "高檔部位，用最緊的MA5法保護獲利，跌破立即出場"
    elif position == "低檔":
        rec = idx_by_method["前波低點法"]
        reason = "低檔剛止跌，給較大空間確認打底，用前波低點法"
    else:
        rec = idx_by_method["MA10法"]
        reason = "行進中部位，用中期MA10法，最常用的移動停利"

    return {
        "recommended": rec["method"],
        "price": rec["price"],
        "distance_pct": rec["distance_pct"],
        "reason": reason,
        "alternatives": alternatives,
    }


# ────────────────────────── 第八章　7步SOP ──────────────────────────


def _build_sop(
    hist: pd.DataFrame,
    li: int,
    wave: dict,
    snapshot: dict,
    ma5: pd.Series,
    ma10: pd.Series,
    ma20: pd.Series,
    ma60: pd.Series,
    weekly: dict,
    stop_loss: dict,
) -> list:
    close_today = float(hist["close"].iloc[li])
    ma5v = float(ma5.iloc[li]) if pd.notna(ma5.iloc[li]) else None
    ma10v = float(ma10.iloc[li]) if pd.notna(ma10.iloc[li]) else None
    ma20v = float(ma20.iloc[li]) if pd.notna(ma20.iloc[li]) else None
    ma60v = float(ma60.iloc[li]) if pd.notna(ma60.iloc[li]) else None

    vals = [v for v in (ma5v, ma10v, ma20v) if v is not None]
    if len(vals) == 3:
        spread_pct = (max(vals) - min(vals)) / min(vals) if min(vals) else 0.0
        if spread_pct < MA_TANGLE_PCT:
            ma_verdict = "糾結"
        elif ma5v > ma10v > ma20v:
            ma_verdict = "多排"
        elif ma5v < ma10v < ma20v:
            ma_verdict = "空排"
        else:
            ma_verdict = "不明確"
        ma_detail = f"MA5={ma5v:.2f} MA10={ma10v:.2f} MA20={ma20v:.2f}（{ma_verdict}）"
    else:
        ma_verdict = "資料不足"
        ma_detail = "均線資料不足"

    if ma60v is not None:
        season_pos = "上方" if close_today > ma60v else "下方"
        ma_detail += f"；季線在股價{season_pos}"
        if li >= MA_SLOPE_LOOKBACK and pd.notna(ma60.iloc[li - MA_SLOPE_LOOKBACK]):
            season_dir = "上揚" if ma60v >= float(ma60.iloc[li - MA_SLOPE_LOOKBACK]) else "下彎"
            ma_detail += f"，{season_dir}"

    vol_today = float(hist["volume"].iloc[li])
    vol20 = hist["volume"].iloc[max(0, li - 19): li + 1]
    vol20ma = float(vol20.mean()) if len(vol20) > 0 else 0.0
    vol_ratio = vol_today / vol20ma if vol20ma > 0 else 0.0

    return [
        {"step": 1, "name": "趨勢", "verdict": wave["trend"], "detail": wave["evidence"]},
        {"step": 2, "name": "均線", "verdict": ma_verdict, "detail": ma_detail},
        {
            "step": 3,
            "name": "位置",
            "verdict": snapshot["position"] or "—",
            "detail": f"{snapshot['position'] or '—'}第{snapshot['leg_days']}天，收盤價領域{snapshot['domain'].get('type') or '—'}",
        },
        {
            "step": 4,
            "name": "K線訊號",
            "verdict": "、".join(snapshot["signals"]) if snapshot["signals"] else "無",
            "detail": snapshot["verdict"] or "—",
        },
        {
            "step": 5,
            "name": "量能",
            "verdict": f"{vol_ratio:.2f}x（第{snapshot['leg_days']}天）",
            "detail": f"今量{vol_today:.0f}／20日均量{vol20ma:.0f}",
        },
        {"step": 6, "name": "週線確認", "verdict": weekly["verdict"], "detail": weekly["detail"]},
        {
            "step": 7,
            "name": "風險",
            "verdict": f"{stop_loss['recommended']}停損{stop_loss['price']}（{_dist_label(stop_loss['distance_pct'])}）",
            "detail": stop_loss["reason"],
        },
    ]


# ────────────────────────── 紀律檢查 ──────────────────────────


def _discipline(snapshot: dict, bias_ma20: float, weekly: dict, wave: dict, rebound_check: dict) -> dict:
    items = []

    high_no_chase = not (snapshot["position"] == "高檔" and snapshot["leg_days"] >= HIGH_LEG_DAYS_MIN)
    items.append({
        "rule": "高檔不追多",
        "pass": high_no_chase,
        "detail": (
            f"位置{snapshot['position'] or '—'}第{snapshot['leg_days']}天，"
            + ("非高檔久盤" if high_no_chase else f"已達{HIGH_LEG_DAYS_MIN}天門檻，不宜追高")
        ),
    })

    bias_ok = bias_ma20 <= BIAS_MA20_HOT_PCT
    items.append({
        "rule": "乖離未過熱",
        "pass": bias_ok,
        "detail": f"對MA20乖離{bias_ma20*100:.1f}%" + ("" if bias_ok else f"，已超過{BIAS_MA20_HOT_PCT*100:.0f}%警戒"),
    })

    no_false_breakout = not any("假突破" in w for w in snapshot["warnings"])
    items.append({
        "rule": "突破需收盤確認",
        "pass": no_false_breakout,
        "detail": "、".join(snapshot["warnings"]) if snapshot["warnings"] else "無假突破警訊",
    })

    weekly_ok = weekly["verdict"] != "衝突"
    items.append({"rule": "長線保護短線", "pass": weekly_ok, "detail": weekly["detail"]})

    not_catch_knife = not (wave["trend"] == "空頭" and rebound_check["passed"] < rebound_check["total"])
    items.append({
        "rule": "不接下跌刀",
        "pass": not_catch_knife,
        "detail": f"趨勢{wave['trend']}，搶反彈四問{rebound_check['passed']}/{rebound_check['total']}通過",
    })

    passed = sum(1 for it in items if it["pass"])
    return {"passed": passed, "total": len(items), "items": items}


# ────────────────────────── 總結 ──────────────────────────


def _build_verdict(
    stock_id: str,
    name: str,
    snapshot: dict,
    wave: dict,
    bottom_stage: dict,
    rebound_check: dict,
    discipline: dict,
    stop_loss: dict,
) -> str:
    parts = [
        f"{name}({stock_id})目前處於{snapshot['position'] or '—'}第{snapshot['leg_days']}天，"
        f"波浪型態{wave['pattern']}（{wave['trend']}）。",
        f"打底四階段目前達「{bottom_stage['stage']}」；搶反彈四問{rebound_check['passed']}/"
        f"{rebound_check['total']}通過（{rebound_check['conclusion']}）。",
        f"紀律檢查{discipline['passed']}/{discipline['total']}項通過。",
    ]
    if discipline["passed"] < discipline["total"]:
        failed_rules = "、".join(it["rule"] for it in discipline["items"] if not it["pass"])
        parts.append(f"未過項目：{failed_rules}，現階段不宜貿然追價，應等待訊號確認或拉回不破再進場。")
    else:
        parts.append("紀律面全數通過，若進場可依SOP紀律執行。")
    parts.append(
        f"若持有部位，建議以{stop_loss['recommended']}停損於{stop_loss['price']}"
        f"（{_dist_label(stop_loss['distance_pct'])}）。"
    )
    if snapshot["warnings"]:
        parts.append(
            "⚠️ " + "；".join(snapshot["warnings"])
            + "。手冊5-4：隔日開盤才是確認——開高則警訊被否決，開低則警訊成立，勿在當下追殺。"
        )
    return "".join(parts)


# ────────────────────────── 對外主函式 ──────────────────────────


def analyze(df: pd.DataFrame, stock_id: str, name: str, idx: int = -1) -> dict:
    """對 df 的第 idx 天（含）以前的資料做完整K線深度分析。
    鐵律：內部一律只用 hist = df.iloc[:idx+1]，不看 idx 之後的任何資料。
    """
    if idx < 0:
        idx = len(df) + idx
    hist = df.iloc[: idx + 1].reset_index(drop=True)
    li = len(hist) - 1

    if len(hist) < MIN_ROWS:
        return {"error": "資料不足"}

    row = hist.iloc[li]
    o, h, l, c, v = (
        float(row["open"]), float(row["high"]), float(row["low"]),
        float(row["close"]), float(row["volume"]),
    )
    prev_c = float(hist["close"].iloc[li - 1]) if li >= 1 else c
    chg_pct = round((c / prev_c - 1) * 100, 2) if prev_c else 0.0
    date_str = pd.Timestamp(row["date"]).strftime("%Y-%m-%d")

    snapshot = detect_kline(hist, li)

    ma5 = hist["close"].rolling(5).mean()
    ma10 = hist["close"].rolling(10).mean()
    ma20 = hist["close"].rolling(20).mean()
    ma60 = hist["close"].rolling(60).mean()

    highs, lows = _find_pivots(hist, li)
    wave = _wave_analysis(highs, lows, c)
    bottom_stage = _bottom_stage(hist, li, wave, ma5, ma10, ma20)
    rebound_check = _rebound_check(hist, li, wave, highs)
    weekly = _weekly_confirm(hist, wave)
    stop_loss = _stop_loss(hist, li, snapshot, ma5, ma10, lows)
    sop = _build_sop(hist, li, wave, snapshot, ma5, ma10, ma20, ma60, weekly, stop_loss)

    ma20v = float(ma20.iloc[li]) if pd.notna(ma20.iloc[li]) else None
    bias_ma20 = (c / ma20v - 1) if ma20v else 0.0
    discipline = _discipline(snapshot, bias_ma20, weekly, wave, rebound_check)

    verdict = _build_verdict(stock_id, name, snapshot, wave, bottom_stage, rebound_check, discipline, stop_loss)

    return {
        "stock_id": stock_id,
        "name": name,
        "date": date_str,
        "price": {"open": o, "high": h, "low": l, "close": c, "volume": v, "chg_pct": chg_pct},
        "snapshot": snapshot,
        "wave": wave,
        "bottom_stage": bottom_stage,
        "rebound_check": rebound_check,
        "sop": sop,
        "discipline": discipline,
        "stop_loss": stop_loss,
        "verdict": verdict,
        "disclaimer": "本分析為手冊框架的機械化判讀，不構成投資建議",
    }
