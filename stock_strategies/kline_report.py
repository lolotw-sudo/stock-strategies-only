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

# ── 7-1　續漲拉回買點 ──
PULLBACK_NEAR_MA_PCT = 0.03    # 收盤距 MA5／MA10 在 3% 內，視為「回測均線」
PULLBACK_BIAS_MAX = 0.10       # 乖離 MA20 ≤10% 才算手冊說的「回到均線附近」

# ── 7-3　創新高四關 ──
BREAKOUT_LEG_DAYS_MAX = 6      # 手冊：第 7 天以後進入加速段，變盤機率大幅升高
BREAKOUT_BIAS_MA20_MAX = 0.20  # 對月線乖離 >+20% → 短線任何風吹草動都可能急殺
BREAKOUT_BIAS_MA120_MAX = 0.50 # 對半年線乖離 >+50% → 極端區
BREAKOUT_VOL_MULT = 1.0        # 突破日價漲量增：量 ≥ 前5日均量

# ── 狀態分流（regime）──
BIAS_HOT_PCT = 0.20            # 乖離 MA20 >20% → 掛🔥過熱旗標，蓋掉該狀態的買點
BULL_TRENDS = {"多頭", "多頭轉弱"}


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
    q1_detail = (
        f"近{REBOUND_WINDOW}日最大回檔{max_dd*100:.1f}%（{peak_val:.1f}→{trough_val:.1f}）"
        f"，門檻{REBOUND_DRAWDOWN_PCT*100:.0f}%"
    )

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


# ────────────────── 5-2-5　強弱勢反彈／壓力區／回撤位 ──────────────────


SWING_LOOKBACK = 60   # 約一季；用 WAVE_LOOKBACK(120) 常跨到上一個循環，關卡會遠到沒有參考價值


def _swing_high_low(hist: pd.DataFrame, li: int) -> tuple | None:
    """找出最近一段「先漲到頂、再跌下來」的波段：近 SWING_LOOKBACK 日內的最高價，
    以及該高點『之後』的最低價。若最低點出現在最高點之前（例如一路創新高），
    代表目前沒有可談的回檔波段，回傳 None。
    """
    start = max(0, li - SWING_LOOKBACK + 1)
    win = hist.iloc[start: li + 1]
    if len(win) < 20:
        return None
    hi_pos = int(win["high"].to_numpy().argmax())
    after = win.iloc[hi_pos:]
    if len(after) < 2:
        return None
    lo_pos_rel = int(after["low"].to_numpy().argmin())
    hi_price = float(win["high"].iloc[hi_pos])
    lo_price = float(after["low"].iloc[lo_pos_rel])
    if hi_price <= lo_price:
        return None
    return (
        hi_price, pd.Timestamp(win["date"].iloc[hi_pos]).strftime("%Y-%m-%d"),
        lo_price, pd.Timestamp(after["date"].iloc[lo_pos_rel]).strftime("%Y-%m-%d"),
    )


def _rebound_strength(hist: pd.DataFrame, li: int, swing: tuple | None,
                      ma10, ma20, highs: list) -> dict | None:
    """5-2-5　強勢反彈 vs 弱勢反彈（四項判準，全部照手冊表格）。

    只有在「從低點反彈上來」的情境下才有意義；沒有回檔波段時回傳 None。
    手冊註記：弱勢反彈的本質是逃命波——不是給你進場的，是給你出場的。
    """
    if not swing:
        return None
    hi_price, _, lo_price, lo_date = swing
    lo_idx = hist.index[hist["date"] == pd.Timestamp(lo_date)]
    if len(lo_idx) == 0:
        return None
    lo_i = int(lo_idx[0])
    days = li - lo_i
    if days < 1:
        return None

    c = float(hist["close"].iloc[li])

    # ① 量能：反彈段均量 vs 反彈前同長度區間均量
    reb_vol = float(hist["volume"].iloc[lo_i: li + 1].mean())
    prev_vol = float(hist["volume"].iloc[max(0, lo_i - days): lo_i].mean()) if lo_i > 0 else 0.0
    vol_ok = prev_vol > 0 and reb_vol > prev_vol
    vol_ratio = reb_vol / prev_vol if prev_vol > 0 else 0.0

    # ② 反彈高度：能不能過前波高點／重要壓力
    # 手冊說的「前波高點」是反彈前那一波的高點——取時間上最接近低點的，
    # 不是區間內最貴的（那可能是好幾個月前的高點，比了沒有意義）
    prior_highs = [h for h in highs if h[0] < lo_i]
    ref_high = max(prior_highs, key=lambda h: h[0])[2] if prior_highs else None
    height_ok = ref_high is not None and c > ref_high

    # ③ 持續性：手冊說弱勢是「一兩天就結束」
    dur_ok = days >= 3

    # ④ 均線：站上並守住 MA10、MA20
    m10 = float(ma10.iloc[li]) if pd.notna(ma10.iloc[li]) else None
    m20 = float(ma20.iloc[li]) if pd.notna(ma20.iloc[li]) else None
    ma_ok = m10 is not None and m20 is not None and c >= m10 and c >= m20

    items = [
        {"rule": "量能放大", "pass": vol_ok,
         "detail": (f"反彈段均量為反彈前 {vol_ratio:.2f} 倍" if prev_vol > 0 else "量能資料不足")
                   + ("" if vol_ok else "，量能萎縮是弱勢特徵")},
        {"rule": "過得了前波高", "pass": height_ok,
         "detail": (f"收盤 {c:.2f} 已站上反彈前高點 {ref_high:.2f}" if height_ok
                    else (f"收盤 {c:.2f} 尚未過反彈前高點 {ref_high:.2f}" if ref_high
                          else "找不到可比對的前波高點"))},
        {"rule": "有持續性", "pass": dur_ok,
         "detail": f"自 {lo_date[5:]} 低點反彈 {days} 個交易日"
                   + ("" if dur_ok else "，一兩天就結束是弱勢特徵")},
        {"rule": "站上並守住均線", "pass": ma_ok,
         "detail": (f"收盤 {c:.2f}／MA10 {m10:.2f}／MA20 {m20:.2f}" if m10 and m20 else "均線資料不足")
                   + ("" if ma_ok else "，碰到均線就被壓回是弱勢特徵")},
    ]
    passed = sum(1 for it in items if it["pass"])
    if passed >= 3:
        verdict, note = "強勢反彈", "四項判準多數成立，反彈有結構。"
    elif passed >= 2:
        verdict, note = "強弱參半", "訊號分歧，手冊建議保守看待。"
    else:
        verdict, note = "弱勢反彈", "手冊 5-2-5：弱勢反彈的本質是逃命波——不是給你進場的，是給你出場的。"
    return {"verdict": verdict, "note": note, "passed": passed, "total": 4, "items": items}


def _resistance_zones(highs: list, close: float, limit: int = 3) -> list:
    """上方壓力關卡（手冊 7-5：前波高點是分批停利的參考位置）。由近而遠。"""
    above = sorted((h for h in highs if h[2] > close), key=lambda h: h[2])
    return [
        {"date": pd.Timestamp(h[1]).strftime("%Y-%m-%d"), "price": round(h[2], 2),
         "gap_pct": round((h[2] / close - 1) * 100, 1)}
        for h in above[:limit]
    ]


FIB_RATIOS = (0.382, 0.5, 0.618)


def _fib_levels(swing: tuple | None, close: float) -> dict | None:
    """波段回撤位。

    註記：Fibonacci 不在《K線訊號判讀手冊》裡，手冊 5-2-5 判斷反彈強弱用的是
    「量能／能否過前波高／持續性／均線」四項。這裡只當作看圖時的輔助刻度，
    不參與任何燈號或檢查表的判定。
    """
    if not swing:
        return None
    hi, hi_date, lo, lo_date = swing
    rng = hi - lo
    if rng <= 0:
        return None
    return {
        "high": round(hi, 2), "high_date": hi_date,
        "low": round(lo, 2), "low_date": lo_date,
        "retrace_pct": round((close - lo) / rng * 100, 1),
        "levels": [{"ratio": r, "price": round(lo + rng * r, 2)} for r in FIB_RATIOS],
        "source_note": "Fibonacci 非手冊內容，僅作看圖輔助刻度，不參與燈號判定",
    }


# ────────────────────── 7-1／7-3　多頭系檢查表 ──────────────────────


def _pullback_check(hist, li, wave, ma5, ma10, bias_ma20) -> dict:
    """7-1　續漲拉回買點：多頭中段回測均線才是買點，不是追在高處。"""
    c = float(hist["close"].iloc[li])
    lo = float(hist["low"].iloc[li])
    m5 = float(ma5.iloc[li]) if pd.notna(ma5.iloc[li]) else None
    m10 = float(ma10.iloc[li]) if pd.notna(ma10.iloc[li]) else None

    aligned = m5 is not None and m10 is not None and m5 > m10
    # 回測 ≠ 跌破：收盤必須仍站在該均線之上（手冊 7-5 把「收盤跌破 MA5／MA10」列為停利訊號，
    # 不可能同時是買點），且要嘛已貼近均線、要嘛當日低點曾觸及均線後被拉回。
    near = []
    for label, m in (("MA5", m5), ("MA10", m10)):
        if m is None:
            continue
        if c >= m and (c / m - 1 <= PULLBACK_NEAR_MA_PCT or lo <= m):
            near.append(f"{label} {m:.2f}")

    items = [
        {"rule": "均線多排", "pass": aligned,
         "detail": f"MA5 {m5:.2f} {'>' if aligned else '≤'} MA10 {m10:.2f}" if m5 and m10 else "均線資料不足"},
        {"rule": "回測均線支撐", "pass": bool(near),
         "detail": f"收盤 {c:.2f} 回測並守住 {'／'.join(near)}" if near
                   else (f"收盤 {c:.2f} 已跌破 MA5 {m5:.2f}／MA10 {m10:.2f}，是停利訊號不是買點"
                         if m5 and m10 and c < min(m5, m10)
                         else f"收盤 {c:.2f} 未回測到 MA5／MA10（門檻 {PULLBACK_NEAR_MA_PCT*100:.0f}%）")},
        {"rule": "乖離回到均線附近", "pass": bias_ma20 <= PULLBACK_BIAS_MAX,
         "detail": f"對MA20乖離 {bias_ma20*100:+.1f}%" +
                   ("" if bias_ma20 <= PULLBACK_BIAS_MAX else f"，超過 {PULLBACK_BIAS_MAX*100:.0f}% 就不是拉回是追高")},
        {"rule": "趨勢未破壞", "pass": not wave["broke_below_last_low"],
         "detail": "尚未跌破前波低" if not wave["broke_below_last_low"] else "已跌破前波低，多頭轉弱"},
    ]
    passed = sum(1 for it in items if it["pass"])
    return {"name": "7-1 續漲拉回買點", "passed": passed, "total": len(items), "items": items}


def _breakout_check(hist, li, wave, snapshot, ma20, ma120, bias_ma20) -> dict:
    """7-3　創新高追價前先過四關。"""
    c = float(hist["close"].iloc[li])
    v = float(hist["volume"].iloc[li])
    leg_days = snapshot["leg_days"]

    m120 = float(ma120.iloc[li]) if pd.notna(ma120.iloc[li]) else None
    bias_120 = (c / m120 - 1) if m120 else 0.0
    bias_ok = bias_ma20 <= BREAKOUT_BIAS_MA20_MAX and bias_120 <= BREAKOUT_BIAS_MA120_MAX

    no_false = not any("假突破" in w for w in snapshot["warnings"])

    prior5 = hist["volume"].iloc[max(0, li - 5): li]
    m5v = float(prior5.mean()) if len(prior5) > 0 else 0.0
    vol_ok = m5v > 0 and v >= BREAKOUT_VOL_MULT * m5v
    if m5v > 0:
        vol_detail = f"今量為前5日均量 {v / m5v:.2f}x" + (
            "（價漲量增）" if vol_ok else "，價漲量縮、追價意願低"
        )
    else:
        vol_detail = "量能資料不足"

    items = [
        {"rule": "①這是第幾天", "pass": leg_days <= BREAKOUT_LEG_DAYS_MAX,
         "detail": f"距波段低點第 {leg_days} 天" +
                   ("（1～3 天續攻合理）" if leg_days <= 3 else
                    "" if leg_days <= BREAKOUT_LEG_DAYS_MAX else "，已進加速段，變盤機率升高")},
        {"rule": "②乖離多少", "pass": bias_ok,
         "detail": f"對MA20 {bias_ma20*100:+.1f}%" +
                   (f"／對半年線 {bias_120*100:+.1f}%" if m120 else "／半年線資料不足") +
                   ("" if bias_ok else "，已達警戒，不宜追高")},
        {"rule": "③收盤確認突破", "pass": wave["broke_above_last_high"] and no_false,
         # 兩個高點基準不同：wave 比的是已確認的「前波轉折高」，假突破警訊比的是「近20日最高」，
         # 因此「站上轉折高」與「假突破」可以同時成立——文案要讓人一看就懂，不能只是並列。
         "detail": (("收盤已站上前波轉折高" if no_false
                     else "收盤雖站上前波轉折高，但" + "、".join(snapshot["warnings"]))
                    if wave["broke_above_last_high"]
                    else "收盤尚未突破前波轉折高" +
                         ("" if no_false else "；" + "、".join(snapshot["warnings"])))},
        {"rule": "④量價配合", "pass": vol_ok, "detail": vol_detail},
    ]
    passed = sum(1 for it in items if it["pass"])
    return {"name": "7-3 創新高四關", "passed": passed, "total": len(items), "items": items}


# ────────────────────────── 狀態分流 ──────────────────────────


def _regime(wave, bottom_stage, bias_ma20) -> dict:
    """判定這檔股票現在的處境，決定要套哪一張檢查表。

    狀態互斥、由上往下先中先算；過熱是可疊加的旗標，不是狀態。
    """
    t = wave["trend"]
    if t == "多頭" and wave["broke_above_last_high"]:
        code, label = "breakout", "創新高"
    elif t in BULL_TRENDS:
        code, label = "uptrend", "多頭行進"
    elif t == "打底突破中" or bottom_stage["stage_index"] >= 3:
        code, label = "confirmed", "反彈確立"
    elif bottom_stage["stage_index"] >= 1:
        code, label = "basing", "打底中"
    else:
        code, label = "downtrend", "空頭續跌"

    hot = bias_ma20 > BIAS_HOT_PCT
    return {
        "code": code,
        "label": label,
        "hot": hot,
        "bias_ma20": round(bias_ma20 * 100, 1),
        "reason": f"波浪型態{wave['trend']}、打底階段{bottom_stage['stage']}" +
                  (f"、對MA20乖離{bias_ma20*100:+.1f}%已過熱" if hot else ""),
    }


# ────────────────────────── 總結 ──────────────────────────


def _nearest_above(pivots: list, price: float):
    """找出價格上方最近的轉折高（突破要跨的那一關）。"""
    above = [p for p in pivots if p["price"] > price]
    return min(above, key=lambda p: p["price"]) if above else None


def _nearest_below(pivots: list, price: float):
    """找出價格下方最近的轉折低（跌破就代表結構壞掉）。"""
    below = [p for p in pivots if p["price"] < price]
    return max(below, key=lambda p: p["price"]) if below else None


def _build_verdict(
    stock_id: str,
    name: str,
    snapshot: dict,
    wave: dict,
    bottom_stage: dict,
    rebound_check: dict,
    discipline: dict,
    stop_loss: dict,
    regime: dict,
    checklist: dict | None,
    close: float,
    ma10v: float | None,
    ma20v: float | None,
    chapter10: dict,
) -> tuple[str, dict]:
    """產生操作結語。

    回傳 (verdict_text, plan)：
      verdict_text —— 純文字，供 Telegram 通知沿用
      plan —— 結構化，供看板排版：
        stance      現在的立場（不進場／可考慮進場／只談停利…）
        headline    一句話講現在的處境（不重複上面已列的分數）
        do          具體該做什麼，附手冊依據
        levels      關鍵價位（要看的數字，一眼可見）
        watch_for   要等的訊號，或這個判斷失效的條件
    """
    # ── 第十章優先：訊號不成立或濾網未過完，一律不得輸出進場結論（10-5 措辭禁令）──
    ch = chapter10
    if ch["conclusion"] != "可考慮進場":
        blocked = "、".join(f["name"] for f in ch["filters"] if not f["pass"])
        plan = {
            "stance": ch["conclusion"],
            "headline": f"結構定性「{ch['structure']['qualified']}」，"
                        f"訊號{'成立' if ch['signal']['long'] else '不成立'}，"
                        f"濾網 {ch['passed']}/{ch['total']}"
                        + (f"（未過：{blocked}）" if blocked else "") + "。",
            "do": "手冊第十章：濾網四項須全數通過，缺一即不進場。"
                  + (f"初始停損只能用訊號K最低點 {ch['stop']['price']:g}（風險 {ch['stop']['risk_pct']:.1f}%），"
                     "不得在進場當下改用前波低點法或型態法。" if ch["signal"]["long"] else ""),
            "levels": ([{"label": "初始停損（訊號K最低點）", "value": ch["stop"]["price"],
                         "note": f"風險 {ch['stop']['risk_pct']:.1f}%"}] if ch["signal"]["long"] else [])
                     + ([{"label": f"第一道壓力（{ch['resistance']['label']}）",
                          "value": ch["resistance"]["price"],
                          "note": f"距現價 +{(ch['resistance']['price'] / close - 1) * 100:.1f}%"}]
                        if ch["resistance"] else []),
            "watch_for": (f"等{ch['wait']} 再重新評估。" if ch["wait"]
                          else "等做多訊號成立（收盤同時突破 MA5 與前一日最高點）。"),
        }
        text = (f"{name}({stock_id})：{plan['headline']} {plan['do']} 後續觀察：{plan['watch_for']}")
        return text, plan

    code = regime["code"]
    hot = regime["hot"]
    bias = regime["bias_ma20"]
    highs = wave.get("recent_highs") or []
    lows = wave.get("recent_lows") or []
    up = _nearest_above(highs, close)
    dn = _nearest_below(lows, close)

    levels: list[dict] = []
    sl_price = stop_loss.get("price")
    sl_broken = (stop_loss.get("distance_pct") or 0) < 0   # 負值＝該方法的參考線已在現價上方，等於已失效
    if sl_price and not sl_broken:
        levels.append({
            "label": f"若已持有，停損／停利（{stop_loss['recommended']}）",
            "value": sl_price,
            "note": _dist_label(stop_loss["distance_pct"]),
        })

    failed = [it for it in (checklist or {}).get("items", []) if not it["pass"]]
    failed_names = "、".join(it.get("rule") or it.get("q") for it in failed)
    disc_failed = "、".join(it["rule"] for it in discipline["items"] if not it["pass"])

    # ── 過熱優先：手冊 7-5，此處談停利不談進場 ──
    if hot:
        stance = "只談停利，不談進場"
        headline = f"{regime['label']}，但對月線乖離 {bias:+.1f}%，已過手冊 20% 的警戒線。"
        do = "手冊 7-5：乖離過大時任何風吹草動都可能引發急殺回補，這裡該做的是分批停利與減碼，不是找買點。"
        if ma20v:
            levels.append({"label": "乖離收斂到 20% 的價位", "value": round(ma20v * 1.20, 2),
                           "note": "跌回這價位以下，過熱才算解除"})
        watch_for = "等乖離收斂、或拉回測試月線不破，再重新評估進場。在那之前只做減碼決策。"

    elif code == "downtrend":
        stance = "不進場"
        headline = f"仍在下跌途中，{bottom_stage['stage']}（{wave['pattern']}）。"
        do = ("手冊第六章列為不該進場的狀態：不接下跌刀，空手等待。"
              + (f"已持有的人，{stop_loss['recommended']}的參考線 {sl_price} 已在現價上方、該方法失效，"
                 "手冊 7-5 的作法是換用更緊的『前一日低點法』控制風險。" if sl_broken and sl_price else ""))
        if dn:
            levels.append({"label": "下方最近的轉折低", "value": dn["price"],
                           "note": f"{dn['date'][5:]}，跌破代表跌勢延續"})
        watch_for = "先等「近10日不再創新低」（止跌成立），再看有沒有爆量搭配長下影或多方吞噬。兩者都到才輪到搶反彈四問。"

    elif code in ("basing", "confirmed"):
        passed = rebound_check["passed"]
        if passed == 4 and not disc_failed:
            stance = "條件已到，可考慮進場"
            headline = f"搶反彈四問全過，紀律亦無礙（{wave['pattern']}）。"
            do = "手冊 5-4：訊號當天記下來，隔天開盤才是答案——開高才進，開低代表訊號沒被確認。"
            watch_for = "進場後若收盤跌破月線，反彈失敗，依停損價出場。"
        else:
            stance = "不進場，等訊號"
            headline = f"{regime['label']}，搶反彈四問 {passed}/4" + (f"（缺：{failed_names}）" if failed_names else "") + "。"
            watch_for = "等未過的那一關成立再進場；跌破下方轉折低則放棄這檔。"
            if any("過高" in (it.get("q") or "") for it in failed) and up:
                do = f"缺的是「過高」這一關：要等收盤站上 {up['price']}（{up['date'][5:]} 轉折高）且量能放大，突破才算數。"
                levels.append({"label": "突破才算數的價位", "value": up["price"],
                               "note": f"{up['date'][5:]} 轉折高，需收盤價站上"})
            elif any("急跌" in (it.get("q") or "") for it in failed):
                do = "缺的是「急跌」：跌幅不夠深，代表這不是搶反彈的場景，硬套四問沒有意義。"
                watch_for = "改看它會不會走成多頭結構——站上月線並做出底底高後，狀態會自動轉為反彈確立或多頭行進，屆時改用別張檢查表。"
            else:
                do = "四問未過完，手冊視為訊號打折扣，此時不宜進場。"
            if dn and not (sl_price and abs(dn["price"] / sl_price - 1) < 0.03):
                levels.append({"label": "打底失敗的價位", "value": dn["price"],
                               "note": f"{dn['date'][5:]} 轉折低，跌破代表打底破功"})

    elif code == "breakout":
        cl_passed = (checklist or {}).get("passed", 0)
        cl_total = (checklist or {}).get("total", 4)
        if cl_passed == cl_total and not disc_failed:
            stance = "可考慮追價，但只能輕部位"
            headline = "創新高，且 7-3 四關全過。"
            do = ("手冊 7-4：不要盤中衝高追、不要漲停排隊追。三種做法擇一——"
                  "等拉回測前高不破再進、或等尾盤確認收在當日上半部、或先進計畫部位的 1/3。")
            watch_for = "手冊 7-1：創新高的停損距離最遠、錯了是大賠，所以部位要輕。跌破突破點就是錯了。"
        else:
            stance = "不追價"
            headline = f"創新高，但 7-3 四關只過 {cl_passed}/{cl_total}" + (f"（未過：{failed_names}）" if failed_names else "") + "。"
            do = "手冊 7-3：四關沒過完就追，等於買在速度最快、停損最遠的位置。這裡不是進場點。"
            watch_for = "等未過的那幾關補齊（通常是乖離收斂或量價重新配合），或等它拉回到均線附近轉為拉回買點。"

    else:  # uptrend
        cl_passed = (checklist or {}).get("passed", 0)
        cl_total = (checklist or {}).get("total", 4)
        if cl_passed == cl_total and not disc_failed:
            stance = "拉回買點成立，可考慮進場"
            headline = f"多頭結構完整（{wave['pattern']}），回測均線有守。"
            do = "手冊 7-1：這是續漲拉回買點，賺的是幅度加速度，停損就在均線腳下。進場採 7-4 做法，別追盤中高點。"
            watch_for = f"收盤跌破 {stop_loss['recommended']}（{stop_loss['price']}）代表拉回變成轉弱" + \
                        (f"；跌破前波低 {dn['price']} 則多頭結構破壞。" if dn else "。")
        else:
            stance = "先不進場"
            reasons = []
            if failed_names:
                reasons.append(f"7-1 未過：{failed_names}")
            if disc_failed:
                reasons.append(f"紀律未過：{disc_failed}")
            headline = f"多頭行進中（{wave['pattern']}），但{'；'.join(reasons)}。"
            broke_ma = any("跌破" in it["detail"] for it in failed)
            if broke_ma:
                do = "收盤已跌破短均線——手冊 7-5 把「收盤跌破 MA5／MA10」列為停利訊號，它不可能同時是買點。有部位的人該看停利，不是加碼。"
            else:
                do = "多頭結構還在，但拉回買點的條件沒到齊，等它真的回測到均線再說。"
            watch_for = f"等收盤重新站回均線之上、且乖離回到 10% 以內，拉回買點才成立。"
            if dn:
                levels.append({"label": "多頭結構失效價", "value": dn["price"],
                               "note": f"{dn['date'][5:]} 前波低，跌破就不是多頭了"})

    plan = {
        "stance": stance,
        "headline": headline,
        "do": do,
        "levels": levels,
        "watch_for": watch_for,
    }

    text_parts = [f"{name}({stock_id})：{headline}", do]
    if levels:
        text_parts.append("關鍵價位——" + "；".join(
            f"{lv['label']} {lv['value']}（{lv['note']}）" for lv in levels))
    text_parts.append("後續觀察：" + watch_for)
    if snapshot["warnings"]:
        text_parts.append(
            "⚠️ " + "；".join(snapshot["warnings"])
            + "。手冊5-4：隔日開盤才是確認——開高則警訊被否決，開低則警訊成立，勿在當下追殺。"
        )
    return " ".join(text_parts), plan


# ═══════════════ 第十章　參數規格章（與前面各章衝突時，以本章為準）═══════════════
#
# 建立原因：2026/09/01 台達電(2308)出現「人工判讀」與「本程式判讀」結論相反——
# 程式說「拉回買點成立，可考慮進場」，隔日 9/02 收黑 −7.24%。追查後確認差異
# 不在手冊內容，而在三處參數空白：①擺動高低點怎麼取樣 ②何時用哪一種停損
# ③濾網沒有可計算的門檻。本節把這三處收斂成唯一解。

CH10_WINDOW = 60              # 10-1　擺動點取樣窗口，不得少於 60 個交易日
CH10_PIVOT_K = 3              # 10-1　高（低）於「前3日與後3日」才算擺動點
CH10_MIN_GAP = 3              # 10-1　相鄰擺動點間隔須 ≥3 日，否則高點取較高者、低點取較低者
CH10_RECENT_N = 3             # 10-2　取最近三個擺動高／低
CH10_MAJOR_N = 4              # 10-2　大級別覆核看最近 4 個已收完月份的高點
CH10_STOP_MAX_PCT = 0.05      # 10-3③ 停損距離上限，超過即訊號作廢
CH10_RR_GOOD = 2.0            # 10-4　濾網二：風報比 ≥2.0 良好
CH10_RR_MIN = 1.5             # 10-4　濾網二：<1.5 不進場
CH10_VOL_MULT = 1.0           # 10-4　濾網三：訊號K量 ≥ 前5日均量 ×1.0
CH10_DRYUP_DAYS = 3           # 10-4　濾網三：連續三日價漲量縮即不通過
CH10_BIAS_MAX = 0.08          # 10-4　濾網四：對 MA20 乖離 >+8% 不進場
CH10_LEG_MAX = 5              # 10-4　濾網四：連續上漲 ≥5 日不進場，等回檔


def _ch10_thin(pivots: list, keep_higher: bool) -> list:
    """10-1　相鄰兩個擺動點間隔 <3 個交易日時只留一個：高點取較高者，低點取較低者。"""
    out: list = []
    for p in pivots:
        if out and p[0] - out[-1][0] < CH10_MIN_GAP:
            if (p[2] > out[-1][2]) if keep_higher else (p[2] < out[-1][2]):
                out[-1] = p
        else:
            out.append(p)
    return out


def _ch10_pivots(hist: pd.DataFrame, li: int) -> tuple[list, list]:
    """10-1　擺動高低點取樣：60 日窗口、左右各 3 日。

    窗口過短會把前一個真高點切出視野外，使「頭頭低」被誤判為「頭頭高」，
    把空頭反彈誤判成多頭回檔——這是最危險的錯判。
    """
    highs, lows = [], []
    start = max(CH10_PIVOT_K, li - CH10_WINDOW + 1)
    for j in range(start, li - CH10_PIVOT_K + 1):
        lo, hi = j - CH10_PIVOT_K, j + CH10_PIVOT_K + 1
        if float(hist["high"].iloc[j]) == float(hist["high"].iloc[lo:hi].max()):
            highs.append((j, hist["date"].iloc[j], float(hist["high"].iloc[j])))
        if float(hist["low"].iloc[j]) == float(hist["low"].iloc[lo:hi].min()):
            lows.append((j, hist["date"].iloc[j], float(hist["low"].iloc[j])))
    return _ch10_thin(highs, True), _ch10_thin(lows, False)


def _ch10_major_review(hist: pd.DataFrame, li: int) -> dict:
    """10-2　大級別覆核：月線高點連續墊低時，日線的多頭只是大空頭中的反彈波。"""
    h = hist.iloc[: li + 1][["date", "high"]].copy()
    per = pd.to_datetime(h["date"]).dt.to_period("M")
    closed = per != per.iloc[-1]           # 當月未收完，不納入
    monthly = h[closed].groupby(per[closed])["high"].max().tail(CH10_MAJOR_N)
    seq = [{"month": str(k), "high": round(float(v), 2)} for k, v in monthly.items()]
    if len(seq) < 3:
        return {"pass": True, "seq": seq, "detail": "已收完的月份不足 3 個，未做大級別覆核"}
    declining = all(seq[i]["high"] < seq[i - 1]["high"] for i in range(1, len(seq)))
    arrow = " → ".join(f"{s['high']:g}" for s in seq)
    return {
        "pass": not declining,
        "seq": seq,
        "detail": f"月線高點 {arrow}" + (
            "，一路墊低——即使日線出現買點，本質仍是反彈，做多買點降一級處理"
            if declining else "，未連續墊低"),
    }


def _ch10_structure(hist: pd.DataFrame, li: int, close: float) -> dict:
    """10-2　結構定性判定表。

    用詞紀律：只有落在「多頭」格才可說多頭結構完整；禁止只憑底底高就宣告多頭。
    「頭頭高／低」以今日收盤是否站上最近一個已確認擺動高判定——這正是 10-2
    所稱「頭頭低尚未突破」的機械化寫法。
    """
    highs, lows = _ch10_pivots(hist, li)
    last_h = highs[-1] if highs else None
    last_l = lows[-1] if lows else None
    prev_l = lows[-2] if len(lows) >= 2 else None

    if last_h is None or prev_l is None:
        return {"qualified": "資料不足", "can_long": False, "highs": [], "lows": [],
                "major": _ch10_major_review(hist, li),
                "detail": f"近 {CH10_WINDOW} 日內確認的擺動高 {len(highs)} 個、擺動低 {len(lows)} 個，不足以定性",
                "confirmed_highs": highs, "confirmed_lows": lows}

    higher_high = close > last_h[2]
    higher_low = False if close < last_l[2] else last_l[2] > prev_l[2]

    if higher_high and higher_low:
        qualified, detail = "多頭", "頭頭高、底底高"
    elif not higher_high and not higher_low:
        qualified, detail = "空頭", "頭頭低、底底低（改參考 5-2-7 放空，不可執行做多買點）"
    elif higher_low:
        qualified, detail = "收斂／未表態", "底底高成立，但頭頭低尚未突破，結構未表態"
    else:
        qualified, detail = "擴張／混亂", "頭頭高、底底低"

    seq = [_fmt_pivot(p) for p in highs[-CH10_RECENT_N:]]
    seq.append({"date": pd.Timestamp(hist["date"].iloc[li]).strftime("%Y-%m-%d"),
                "price": close, "tentative": True})
    major = _ch10_major_review(hist, li)
    return {
        "qualified": qualified,
        "can_long": qualified == "多頭" and major["pass"],
        "highs": seq,
        "lows": [_fmt_pivot(p) for p in lows[-CH10_RECENT_N:]],
        "major": major,
        "detail": detail + f"（今日收盤 {close:g} vs 最近確認擺動高 {last_h[2]:g}）",
        "confirmed_highs": highs, "confirmed_lows": lows,
    }


def _ch10_signal(hist: pd.DataFrame, li: int, ma5v: float | None, close: float) -> dict:
    """10-5　訊號定義：做多＝收盤突破MA5 且 收盤突破前一日最高點。"""
    ph = float(hist["high"].iloc[li - 1])
    pl = float(hist["low"].iloc[li - 1])
    long_ok = ma5v is not None and close > ma5v and close > ph
    short_ok = ma5v is not None and close < ma5v and close < pl
    if ma5v is None:
        detail = "MA5 資料不足"
    elif long_ok:
        detail = f"收 {close:g} > MA5 {ma5v:.0f}，且 > 前日高 {ph:g}"
    else:
        miss = []
        if close <= ma5v:
            miss.append(f"未站上 MA5 {ma5v:.0f}")
        if close <= ph:
            miss.append(f"未過前日高 {ph:g}")
        detail = f"收 {close:g}：" + "、".join(miss)
    return {"long": bool(long_ok), "short": bool(short_ok), "detail": detail}


def _ch10_first_resistance(close: float, ma60v: float | None,
                           confirmed_highs: list, fib: dict | None) -> dict | None:
    """10-4　第一道壓力：季線／前一個擺動高／Fibonacci 回撤滿足點／整數關卡，
    取「高於進場價且距離最近」者。"""
    cands: list[tuple[float, str]] = []
    if ma60v and ma60v > close:
        cands.append((round(ma60v, 2), "季線"))
    above_h = [p for p in confirmed_highs if p[2] > close]
    if above_h:
        p = min(above_h, key=lambda x: x[2])
        cands.append((p[2], f"{pd.Timestamp(p[1]).strftime('%m/%d')} 擺動高"))
    for lv in (fib or {}).get("levels", []):
        if lv["price"] > close:
            cands.append((lv["price"], f"{lv['ratio']:.3f} 回撤位"))
    step = 100 if close >= 1000 else 10 if close >= 100 else 5
    cands.append((float(int(close // step) * step + step), "整數關卡"))
    if not cands:
        return None
    price, label = min(cands, key=lambda x: x[0])
    return {"price": price, "label": label}


def _ch10_filters(hist: pd.DataFrame, li: int, close: float, structure: dict,
                  resistance: dict | None, stop_price: float, bias_ma20: float) -> list:
    """10-4　前提濾網四項，須全數通過，缺一即不進場。"""
    # 一　結構
    maj = structure["major"]
    f1 = {"name": "結構", "pass": structure["can_long"],
          "detail": f"定性：{structure['qualified']}——{structure['detail']}；"
                    f"大級別覆核{'通過' if maj['pass'] else '未通過'}（{maj['detail']}）"}

    # 二　風報比
    risk = close - stop_price
    if resistance and risk > 0:
        rr = (resistance["price"] - close) / risk
        verdict = "良好" if rr >= CH10_RR_GOOD else "勉強可，部位減半" if rr >= CH10_RR_MIN else "不進場"
        f2 = {"name": "風報比", "pass": rr >= CH10_RR_MIN, "value": round(rr, 2),
              "detail": f"第一道壓力{resistance['label']} {resistance['price']:g}；"
                        f"({resistance['price']:g}−{close:g})÷({close:g}−{stop_price:g})＝{rr:.2f} → {verdict}"}
    else:
        f2 = {"name": "風報比", "pass": False, "value": None,
              "detail": ("上方找不到可辨識的壓力（季線／前波高／回撤位／整數關卡皆不在現價之上），無法計算風報比"
                         if not resistance else
                         f"當日最低點等於收盤 {close:g}，停損距離為零，風報比無定義")}

    # 三　量能
    v = float(hist["volume"].iloc[li])
    prior5 = hist["volume"].iloc[max(0, li - 5): li]
    m5v = float(prior5.mean()) if len(prior5) else 0.0
    vol_ok = m5v > 0 and v >= CH10_VOL_MULT * m5v
    dry = all(
        float(hist["close"].iloc[li - k]) > float(hist["close"].iloc[li - k - 1])
        and float(hist["volume"].iloc[li - k]) < float(hist["volume"].iloc[li - k - 1])
        for k in range(CH10_DRYUP_DAYS)
    ) if li >= CH10_DRYUP_DAYS else False
    f3 = {"name": "量能", "pass": bool(vol_ok and not dry),
          "detail": (f"訊號K量為前5日均量 {v / m5v:.2f}x" if m5v else "量能資料不足")
                    + ("" if vol_ok else "，未達 1.0x")
                    + ("；且連續三日價漲量縮" if dry else "")}

    # 四　乖離與漲勢天數
    up_days = 0
    while li - up_days >= 1 and float(hist["close"].iloc[li - up_days]) > float(hist["close"].iloc[li - up_days - 1]):
        up_days += 1
    bias_ok = bias_ma20 <= CH10_BIAS_MAX
    days_ok = up_days < CH10_LEG_MAX
    f4 = {"name": "乖離", "pass": bool(bias_ok and days_ok), "up_days": up_days,
          "detail": f"對 MA20 乖離 {bias_ma20 * 100:+.1f}%"
                    + ("" if bias_ok else f"，超過 {CH10_BIAS_MAX * 100:.0f}% 門檻")
                    + f"；連續上漲第 {up_days} 日"
                    + ("" if days_ok else f"，已達 {CH10_LEG_MAX} 日，等回檔")}
    return [f1, f2, f3, f4]


def _chapter10(hist: pd.DataFrame, li: int, close: float, ma5v: float | None,
               ma60v: float | None, bias_ma20: float, fib: dict | None) -> dict:
    """10-5　輸出格式規範：結構／訊號／濾網／停損／結論，缺項視為判讀未完成。"""
    structure = _ch10_structure(hist, li, close)
    signal = _ch10_signal(hist, li, ma5v, close)

    # 10-3①　初始停損唯一方法＝訊號K最低點；禁止在進場當下使用前波低點法／型態法
    stop_price = float(hist["low"].iloc[li])
    risk_pct = round((close - stop_price) / close * 100, 2) if close else 0.0
    stop_void = risk_pct > CH10_STOP_MAX_PCT * 100
    stop = {"price": round(stop_price, 2), "risk_pct": risk_pct, "void": bool(stop_void),
            "applicable": bool(signal["long"]),
            "detail": (f"訊號K最低點 {stop_price:g}，風險 {risk_pct:.1f}%"
                       + (f"——超過 {CH10_STOP_MAX_PCT * 100:.0f}% 上限，訊號作廢不進場，等下一根"
                          if stop_void else "（收盤跌破即出場，不看盤中）"))
                      if signal["long"] else
                      f"今日非訊號K，沒有初始停損可設（若成立會落在當日最低點 {stop_price:g}）"}

    # confirmed_* 是內部用的原始 pivot（含 Timestamp，不可序列化），取完壓力就移除
    resistance = _ch10_first_resistance(close, ma60v, structure.pop("confirmed_highs"), fib)
    structure.pop("confirmed_lows", None)
    filters = _ch10_filters(hist, li, close, structure, resistance, stop_price, bias_ma20)
    passed = sum(1 for f in filters if f["pass"])

    # 10-5　結論：措辭禁令——濾網未全過時不得輸出「可考慮進場」
    if not signal["long"]:
        conclusion, wait = "無進場訊號", None
    elif stop_void:
        conclusion, wait = "訊號成立但不進場，等下一根", "訊號K實體過長，停損放不下"
    elif passed == len(filters):
        conclusion, wait = "可考慮進場", None
    else:
        blocked = [f["name"] for f in filters if not f["pass"]]
        if resistance and ({"結構", "風報比"} & set(blocked)):
            wait = f"收盤站上{resistance['label']} {resistance['price']:g}"
        elif "量能" in blocked:
            wait = "量能放大到 5 日均量之上"
        else:
            wait = "回檔收斂乖離與漲勢天數"
        conclusion = f"訊號成立但不進場，等{wait}"

    return {
        "structure": structure,
        "signal": signal,
        "filters": filters,
        "passed": passed,
        "total": len(filters),
        "stop": stop,
        "resistance": resistance,
        "conclusion": conclusion,
        "wait": wait,
        "basis": "手冊第十章（參數規格章）；本章與前面各章衝突時以本章為準",
    }


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
    ma120 = hist["close"].rolling(120).mean()   # 半年線，7-3 第②關用

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

    regime = _regime(wave, bottom_stage, bias_ma20)

    swing = _swing_high_low(hist, li)
    resistance = _resistance_zones(highs, c)
    fib = _fib_levels(swing, c)
    # 5-2-5 屬第五章之二「反彈專章」，只適用於從低點反彈上來的股票；
    # 已走成多頭或創新高的股票談反彈強弱沒有意義，不計算。
    rebound_strength = (
        _rebound_strength(hist, li, swing, ma10, ma20, highs)
        if regime["code"] in ("basing", "confirmed") else None
    )
    if regime["code"] == "breakout":
        checklist = _breakout_check(hist, li, wave, snapshot, ma20, ma120, bias_ma20)
    elif regime["code"] == "uptrend":
        checklist = _pullback_check(hist, li, wave, ma5, ma10, bias_ma20)
    elif regime["code"] in ("confirmed", "basing"):
        checklist = dict(rebound_check, name="5-2-0 搶反彈四問")
    else:
        checklist = None   # 空頭續跌：手冊第六章 ⚪ 不進場，沒有買點檢查表

    ma10v = float(ma10.iloc[li]) if pd.notna(ma10.iloc[li]) else None
    ma60v = float(ma60.iloc[li]) if pd.notna(ma60.iloc[li]) else None
    ma5v = float(ma5.iloc[li]) if pd.notna(ma5.iloc[li]) else None
    # 第十章是參數規格章，與前面各章衝突時以它為準——因此它產出的結論凌駕
    # 第六／七章的檢查表結論，_build_verdict 只在它放行時才沿用原本的文案。
    chapter10 = _chapter10(hist, li, c, ma5v, ma60v, bias_ma20, fib)
    verdict, plan = _build_verdict(
        stock_id, name, snapshot, wave, bottom_stage, rebound_check, discipline,
        stop_loss, regime, checklist, c, ma10v, ma20v, chapter10,
    )

    return {
        "stock_id": stock_id,
        "name": name,
        "date": date_str,
        "price": {"open": o, "high": h, "low": l, "close": c, "volume": v, "chg_pct": chg_pct},
        "snapshot": snapshot,
        "wave": wave,
        "bottom_stage": bottom_stage,
        "rebound_check": rebound_check,
        "regime": regime,
        "chapter10": chapter10,
        "checklist": checklist,
        "rebound_strength": rebound_strength,
        "resistance": resistance,
        "fib": fib,
        "sop": sop,
        "discipline": discipline,
        "stop_loss": stop_loss,
        "verdict": verdict,
        "plan": plan,
        "disclaimer": "本分析為手冊框架的機械化判讀，不構成投資建議",
    }
