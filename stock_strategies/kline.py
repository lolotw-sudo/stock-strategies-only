"""朱家泓K線技術分析模組（V1 第一階段）

依據使用者手冊《K線訊號判讀手冊》：
- 第四章　K線訊號分類（單K / 雙K / 三K）
- 第五章　收盤價訊號法（買方/賣方領域）、5-3 位置×天數鐵律、5-4 隔日開盤確認
- 7-6　南亞科(2408) 案例（假突破 + 換手警訊的驗證基準）

風格仿 volume.py：提供 detect_kline(df, idx) 回傳 dict + verdict 文字。
df 欄位需為小寫 open/high/low/close/volume（與 volume.py 同）。
"""

import pandas as pd

MIN_ROWS = 60

# ── 5-2　收盤價領域門檻 ──
DOMAIN_STRONG = 0.80   # 強勢控盤 / 強勢賣壓
DOMAIN_LEAN = 0.60     # 偏多 / 偏空

DOMAIN_BONUS = {
    "強勢控盤": 8,
    "偏多": 4,
    "拉鋸": 0,
    "偏空": -4,
    "強勢賣壓": -8,
}

# ── 4-1　單K門檻 ──
LONG_BODY_OPEN_PCT = 0.025   # 長紅/長黑：實體/開盤價 ≥ 2.5%
LONG_BODY_RANGE_PCT = 0.60   # 且 實體/全距 ≥ 60%
DOJI_RANGE_PCT = 0.10        # 十字線：實體/全距 ≤ 10%
SHADOW_MULT = 2.0            # 錘子/流星：長影線 ≥ 2x 實體、短影線 ≤ 1x 實體

# ── 4-3　三K（晨星/夜星）門檻 ──
STAR_MID_BODY_RANGE_PCT = 0.30   # 中間小實體：body/range < 30%

# ── 5-3　位置門檻（60日視窗）──
POSITION_WINDOW = 60
POSITION_HIGH_PCT = 0.05         # 距60日最高收盤 ≤5% → 高檔
POSITION_LOW_RETRACE_PCT = 0.20  # 自60日高點回檔 ≥20% → 低檔
POSITION_LOW_NEAR_PCT = 0.05     # 距60日最低收盤 ≤5% → 低檔

# ── leg_days / 量能門檻 ──
LEG_WINDOW = 20              # 近20日最低 low 決定「行情走第幾天」
VOLUME_BREAKOUT_MULT = 2.0   # 爆量：今量 ≥ 2x 前5日均量
VOLUME_WINDOW = 20           # 天量：近20日（含今日）最大；假突破：前20日最高 high

# ── 加減分（手冊第一章：位置決定意義）──
BONUS_BULLISH_LOW = 15
BONUS_BULLISH_OTHER = 5
BONUS_BEARISH_HIGH = -15
BONUS_BEARISH_OTHER = -5
BONUS_LOW_VOL_LONGRED = 10   # 低檔＋爆量＋長紅K 額外加分（主力進場）
BONUS_CHURN_WARNING = -20    # 換手警訊
BONUS_FALSE_BREAKOUT = -20   # 假突破
BONUS_CLAMP = (-40, 30)

BULLISH_SIGNALS = {"錘子", "多方吞噬", "貫穿線", "晨星", "三白兵"}
BEARISH_SIGNALS = {"流星", "空方吞噬", "烏雲罩頂", "夜星", "三烏鴉"}

_EMPTY_RESULT = {
    "signals": [],
    "bonus": 0,
    "domain": {"buyer_pct": 0.0, "seller_pct": 0.0, "type": ""},
    "position": "",
    "leg_days": 0,
    "warnings": [],
    "verdict": "",
}


def _is_long_red(o: float, c: float, body: float, rng: float) -> bool:
    return (
        c > o
        and rng > 0
        and o > 0
        and body / o >= LONG_BODY_OPEN_PCT
        and body / rng >= LONG_BODY_RANGE_PCT
    )


def _is_long_black(o: float, c: float, body: float, rng: float) -> bool:
    return (
        c < o
        and rng > 0
        and o > 0
        and body / o >= LONG_BODY_OPEN_PCT
        and body / rng >= LONG_BODY_RANGE_PCT
    )


def detect_kline(df: pd.DataFrame, idx: int = -1) -> dict:
    """
    偵測收盤價領域 + 單K/雙K/三K訊號 + 位置×天數鐵律。
    回傳: {"signals": [...], "bonus": int, "domain": {...}, "position": str,
           "leg_days": int, "warnings": [...], "verdict": str}
    """
    if len(df) < MIN_ROWS:
        return dict(_EMPTY_RESULT)

    if idx < 0:
        idx = len(df) + idx

    o = float(df["open"].iloc[idx])
    h = float(df["high"].iloc[idx])
    l = float(df["low"].iloc[idx])
    c = float(df["close"].iloc[idx])
    v = float(df["volume"].iloc[idx])
    rng = h - l
    body = abs(c - o)

    signals: list[str] = []
    warnings: list[str] = []
    bonus = 0

    # ── 5-2　收盤價領域（range=0 時跳過，如鎖死漲跌停）──
    domain = {"buyer_pct": 0.0, "seller_pct": 0.0, "type": ""}
    if rng > 0:
        buyer_pct = (c - l) / rng
        seller_pct = (h - c) / rng
        if buyer_pct >= DOMAIN_STRONG:
            dtype = "強勢控盤"
        elif buyer_pct >= DOMAIN_LEAN:
            dtype = "偏多"
        elif seller_pct >= DOMAIN_STRONG:
            dtype = "強勢賣壓"
        elif seller_pct >= DOMAIN_LEAN:
            dtype = "偏空"
        else:
            dtype = "拉鋸"
        domain = {
            "buyer_pct": round(buyer_pct * 100, 1),
            "seller_pct": round(seller_pct * 100, 1),
            "type": dtype,
        }
        bonus += DOMAIN_BONUS[dtype]

    # ── 5-3　位置（60日收盤價視窗）──
    win_start = max(0, idx - POSITION_WINDOW + 1)
    win_close = df["close"].iloc[win_start: idx + 1]
    high_close = float(win_close.max())
    low_close = float(win_close.min())

    position = "行進中"
    if high_close > 0 and (1 - c / high_close) <= POSITION_HIGH_PCT:
        position = "高檔"
    elif high_close > 0 and (1 - c / high_close) >= POSITION_LOW_RETRACE_PCT:
        position = "低檔"
    elif low_close > 0 and (c / low_close - 1) <= POSITION_LOW_NEAR_PCT:
        position = "低檔"

    # ── leg_days：今日 idx 減去近20日最低 low 的 idx ──
    leg_start = max(0, idx - LEG_WINDOW + 1)
    win_low = df["low"].iloc[leg_start: idx + 1]
    min_low_pos = leg_start + int(win_low.to_numpy().argmin())
    leg_days = idx - min_low_pos

    # ── 量能：爆量（今量≥2x前5日均量）／天量（近20日含今日最大）──
    vol5_prior = df["volume"].iloc[max(0, idx - 5): idx]
    vol5ma = float(vol5_prior.mean()) if len(vol5_prior) > 0 else 0.0
    is_breakout_vol = vol5ma > 0 and v >= VOLUME_BREAKOUT_MULT * vol5ma

    vol20_start = max(0, idx - VOLUME_WINDOW + 1)
    vol20 = df["volume"].iloc[vol20_start: idx + 1]
    is_huge_vol = len(vol20) > 0 and v >= float(vol20.max())

    # ── 4-1　單K（互斥：長紅/長黑/十字線/錘子/流星，擇一）──
    if rng > 0:
        if _is_long_red(o, c, body, rng):
            signals.append("長紅K")
        elif _is_long_black(o, c, body, rng):
            signals.append("長黑K")
        elif body / rng <= DOJI_RANGE_PCT:
            signals.append("十字線")
        else:
            lower_shadow = min(o, c) - l
            upper_shadow = h - max(o, c)
            if lower_shadow >= SHADOW_MULT * body and upper_shadow <= body:
                signals.append("錘子")
            elif upper_shadow >= SHADOW_MULT * body and lower_shadow <= body:
                signals.append("流星")

    # ── 4-2　雙K（今日與昨日）──
    if idx >= 1:
        po = float(df["open"].iloc[idx - 1])
        ph = float(df["high"].iloc[idx - 1])
        pl = float(df["low"].iloc[idx - 1])
        pc = float(df["close"].iloc[idx - 1])
        prng = ph - pl
        pbody = abs(pc - po)

        if c > o and pc < po and o <= pc and c >= po:
            signals.append("多方吞噬")
        elif c < o and pc > po and o >= pc and c <= po:
            signals.append("空方吞噬")
        elif pbody > 0 and max(o, c) <= max(po, pc) and min(o, c) >= min(po, pc):
            # 母子線（孕線）：變盤前兆，中性，不加減分
            signals.append("母子線")

        mid_prev = (po + pc) / 2
        if _is_long_black(po, pc, pbody, prng) and o < pc and mid_prev < c < po:
            signals.append("貫穿線")
        if _is_long_red(po, pc, pbody, prng) and o > pc and po < c < mid_prev:
            signals.append("烏雲罩頂")

    # ── 4-3　三K（今日、昨日、前日）──
    if idx >= 2:
        p2o = float(df["open"].iloc[idx - 2])
        p2h = float(df["high"].iloc[idx - 2])
        p2l = float(df["low"].iloc[idx - 2])
        p2c = float(df["close"].iloc[idx - 2])
        p2rng = p2h - p2l
        p2body = abs(p2c - p2o)

        po = float(df["open"].iloc[idx - 1])
        pc = float(df["close"].iloc[idx - 1])
        pl_ = float(df["low"].iloc[idx - 1])
        ph_ = float(df["high"].iloc[idx - 1])
        prng = ph_ - pl_
        pbody = abs(pc - po)
        mid_small = prng > 0 and pbody / prng < STAR_MID_BODY_RANGE_PCT

        if (
            _is_long_black(p2o, p2c, p2body, p2rng)
            and mid_small
            and _is_long_red(o, c, body, rng)
            and c > (p2o + p2c) / 2
        ):
            signals.append("晨星")

        if (
            _is_long_red(p2o, p2c, p2body, p2rng)
            and mid_small
            and _is_long_black(o, c, body, rng)
            and c < (p2o + p2c) / 2
        ):
            signals.append("夜星")

        c2, c1, c0 = p2c, pc, c
        o2, o1, o0 = p2o, po, o
        if c2 > o2 and c1 > o1 and c0 > o0 and c1 > c2 and c0 > c1:
            signals.append("三白兵")
        if c2 < o2 and c1 < o1 and c0 < o0 and c1 < c2 and c0 < c1:
            signals.append("三烏鴉")

    # ── 位置決定意義：多方/空方訊號加減分 ──
    for s in signals:
        if s in BULLISH_SIGNALS:
            bonus += BONUS_BULLISH_LOW if position == "低檔" else BONUS_BULLISH_OTHER
        elif s in BEARISH_SIGNALS:
            bonus += BONUS_BEARISH_HIGH if position == "高檔" else BONUS_BEARISH_OTHER

    # 低檔＋爆量＋長紅K：主力進場，額外加分
    if position == "低檔" and is_breakout_vol and "長紅K" in signals:
        bonus += BONUS_LOW_VOL_LONGRED

    # 換手警訊：高檔、走了≥3天、爆天量，卻只換來拉鋸/偏空/強勢賣壓收盤
    if (
        position == "高檔"
        and leg_days >= 3
        and is_huge_vol
        and domain.get("type") in {"拉鋸", "偏空", "強勢賣壓"}
    ):
        bonus += BONUS_CHURN_WARNING
        warnings.append(f"爆量不漲是換手（高檔第{leg_days}天爆天量）")

    # 假突破：盤中過前20日高、收盤未過，且賣方領域≥80%
    prior_start = max(0, idx - VOLUME_WINDOW)
    if idx > 0:
        prior_high = float(df["high"].iloc[prior_start:idx].max())
        seller_pct = domain["seller_pct"] / 100 if domain.get("type") else 0.0
        if h > prior_high and c < prior_high and seller_pct >= DOMAIN_STRONG:
            bonus += BONUS_FALSE_BREAKOUT
            warnings.append(f"假突破（破前高收回，賣方領域{domain['seller_pct']:.0f}%）")

    bonus = max(BONUS_CLAMP[0], min(BONUS_CLAMP[1], bonus))

    verdict_str = _verdict(signals, bonus, domain, position, leg_days, warnings)

    return {
        "signals": signals,
        "bonus": bonus,
        "domain": domain,
        "position": position,
        "leg_days": leg_days,
        "warnings": warnings,
        "verdict": verdict_str,
    }


def _verdict(
    signals: list[str],
    bonus: int,
    domain: dict,
    position: str,
    leg_days: int,
    warnings: list[str],
) -> str:
    """根據偵測結果給出一句繁中結論（手冊5-4：隔日開盤才是確認）"""
    parts = []
    if domain.get("type"):
        parts.append(
            f"收盤價領域{domain['type']}（買方{domain['buyer_pct']:.0f}% / "
            f"賣方{domain['seller_pct']:.0f}%）"
        )
    parts.append("訊號：" + "、".join(signals) if signals else "無明顯K線訊號")
    parts.append(f"位置：{position}第{leg_days}天")
    verdict_str = "，".join(parts) + "。"
    if warnings:
        verdict_str += "⚠️ " + "；".join(warnings) + "。"
        verdict_str += "⏰ 隔日開盤確認：開高→警訊否決；開低→警訊成立"
    return verdict_str
