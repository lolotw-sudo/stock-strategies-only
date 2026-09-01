from datetime import datetime
from typing import Optional

import pandas as pd

from .config import CONFIG
from .data import get_fundamental, get_price_history
from .indicators import add_indicators, tech_score_at
from .backtest import backtest
from .volume import detect_patterns, verdict as volume_verdict
from .kline import detect_kline
from .kline_report import analyze as analyze_kline_sop
from .loader import merge_params


def _consecutive_days_below_ma20(px: pd.DataFrame) -> int:
    """從今天往回數，收盤持續在 MA20 下方的交易日數。只用已存在的歷史資料，不看未來。"""
    n = 0
    for i in range(len(px) - 1, -1, -1):
        ma20v = px["ma20"].iloc[i]
        if pd.isna(ma20v) or not (px["close"].iloc[i] < ma20v):
            break
        n += 1
    return n


def _discipline_state(disc: dict, exclude: tuple = ()) -> tuple[int, int, list]:
    """回傳 (通過數, 應檢數, 未過項目)。exclude 用來排除已被該狀態專屬檢查表取代的紀律。"""
    items = [it for it in disc["items"] if it["rule"] not in exclude]
    failed = [it for it in items if not it["pass"]]
    return len(items) - len(failed), len(items), failed


def _decide_kline_sop_action(
    px: pd.DataFrame, latest: pd.Series, sop_report: dict, risk_notes: list
) -> tuple[str, str]:
    """依《K線訊號判讀手冊》先判狀態、再套對應檢查表決定 action（不依賴加權分數）。

    優先序：
      ① SELL：今日收盤跌破 MA20，且昨日收盤仍在 MA20（含）以上（最高優先，蓋過其他）
      ② 🔥過熱（乖離MA20 >20%）：不給 BUY，改 WATCH 並附 7-5 停利提示
      ③ 依 regime 分流：
         空頭續跌  → SKIP（手冊第六章 ⚪ 不進場）
         打底中／反彈確立 → 5-2-0 搶反彈四問（原邏輯）
         多頭行進  → 7-1 續漲拉回買點
         創新高    → 7-3 創新高四關
      任何狀態的 BUY 都要求「檢查表全過 + 紀律全過 + 收盤站上 MA20」。
    """
    today_close = float(latest["close"])
    ma20_today = latest.get("ma20")
    has_ma20 = bool(pd.notna(ma20_today))
    ma20_val = float(ma20_today) if has_ma20 else None

    prev_below = None
    if has_ma20 and len(px) >= 2:
        prev_ma20 = px["ma20"].iloc[-2]
        if pd.notna(prev_ma20):
            prev_below = float(px["close"].iloc[-2]) < float(prev_ma20)

    below_today = has_ma20 and today_close < ma20_val
    is_new_cross = below_today and prev_below is False  # 昨日確定未破，今日確定跌破 → 新跌破

    if is_new_cross:
        risk_notes.append(f"跌破月線 MA20（{ma20_val:.2f}），持有者出場訊號")
        return "SELL", f"跌破月線 MA20 {ma20_val:.2f}"

    if below_today:
        n_days = _consecutive_days_below_ma20(px)
        risk_notes.append(f"已在月線下方 {n_days} 日")

    regime = sop_report["regime"]
    disc = sop_report["discipline"]
    above_ma20_now = has_ma20 and today_close >= ma20_val
    label = regime["label"]

    # ② 過熱旗標蓋掉買點（手冊 7-5：這裡談的是停利，不是進場）
    if regime["hot"]:
        risk_notes.append(
            f"對MA20乖離 {regime['bias_ma20']:+.1f}% 已過熱（>20%），"
            "手冊 7-5：此處該談停利與減碼，不是進場"
        )
        return "WATCH", f"{label}但乖離{regime['bias_ma20']:+.1f}%過熱，只看停利不進場"

    # ③ 空頭續跌：手冊第六章 ⚪ 不該進場的狀態
    if regime["code"] == "downtrend":
        return "SKIP", f"{label}（{regime['reason']}），手冊：此時不進場"

    checklist = sop_report.get("checklist")
    if not checklist:
        return "SKIP", f"{label}，無對應買點檢查表"

    # 創新高狀態下，「高檔不追多」由 7-3 的①天數②乖離取代（手冊 7-3 專講創新高怎麼追），
    # 否則以「距60日高點≤5%」為準的高檔判定會讓創新高永遠無法成立。
    exclude = ("高檔不追多",) if regime["code"] == "breakout" else ()
    d_passed, d_total, d_failed = _discipline_state(disc, exclude)

    cl_name = checklist["name"]
    cl_passed, cl_total = checklist["passed"], checklist["total"]
    full = cl_passed == cl_total

    if full and d_passed == d_total and above_ma20_now:
        return "BUY", f"{label}：{cl_name}{cl_passed}/{cl_total}、紀律{d_passed}/{d_total}全過，站上月線"

    if full:
        if d_passed < d_total:
            for it in d_failed:
                risk_notes.append(f"紀律未過：{it['rule']}——{it['detail']}")
            return "WATCH", f"{label}：{cl_name}全過，但紀律{d_passed}/{d_total}未全過"
        note = (f"未站上月線 MA20（{ma20_val:.2f}），暫緩列為BUY" if has_ma20
                else "月線資料不足，暫緩列為BUY")
        risk_notes.append(note)
        return "WATCH", f"{label}：{cl_name}全過且紀律全過，但尚未站上月線"

    if cl_passed == cl_total - 1:
        for it in checklist["items"]:
            if not it["pass"]:
                risk_notes.append(f"{cl_name}未過：{it.get('rule') or it.get('q')}——{it['detail']}")
        return "WATCH", f"{label}：{cl_name}{cl_passed}/{cl_total}通過"

    return "SKIP", f"{label}：{cl_name}僅{cl_passed}/{cl_total}通過，不建議進場"


def evaluate(stock_id: str, name: str, strategy: dict | None = None) -> Optional[dict]:
    """評估一檔股票。strategy 為策略 dict（含 params），不給就用預設值。"""
    params = merge_params(strategy)

    result = {
        "stock_id": stock_id,
        "name": name,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "strategy_id": (strategy or {}).get("id", "default"),
        "risk_notes": [],
    }

    try:
        fund = get_fundamental(stock_id)
        eps_vals = list(fund["eps"].values())
        roe_vals = list(fund["roe"].values())
        fund_pass = (
            len(eps_vals) >= 2
            and len(roe_vals) >= 2
            and min(eps_vals) > params["eps_threshold"]
            and min(roe_vals) > params["roe_threshold"]
        )

        px = get_price_history(stock_id, params["backtest_years"])
        if len(px) < 100:
            result["action"] = "SKIP"
            result["risk_notes"].append("價格資料不足")
            return result

        px = add_indicators(px)
        latest = px.iloc[-1]
        ts = tech_score_at(latest, params)
        bt = backtest(px, params)

        if params["use_volume_patterns"]:
            vp = detect_patterns(px)
        else:
            vp = {"patterns": [], "bonus": 0, "details": {}}

        if params["use_kline_signals"]:
            kl = detect_kline(px)
        else:
            kl = {
                "signals": [], "bonus": 0,
                "domain": {"buyer_pct": 0.0, "seller_pct": 0.0, "type": ""},
                "position": "", "leg_days": 0, "warnings": [], "verdict": "",
            }

        fund_score = 100 if fund_pass else 40
        tech_score = max(0, min(100, ts["score"] + vp["bonus"] + kl["bonus"]))
        winrate = bt.get("winrate") or 0.5
        bt_score = winrate * 100

        wf = params["weight_fundamental"]
        wt = params["weight_technical"]
        wb = params["weight_backtest"]
        # 正規化權重
        wsum = wf + wt + wb
        if wsum > 0:
            wf, wt, wb = wf / wsum, wt / wsum, wb / wsum

        signal_score = round(wf * fund_score + wt * tech_score + wb * bt_score, 1)

        action_mode = params.get("action_mode", "score")
        if action_mode not in ("score", "kline_sop"):
            action_mode = "score"

        action_reason = None
        sop_report = None
        if action_mode == "kline_sop":
            sop_report = analyze_kline_sop(px, stock_id, name)

        if action_mode == "kline_sop" and sop_report and "error" not in sop_report:
            action, action_reason = _decide_kline_sop_action(
                px, latest, sop_report, result["risk_notes"]
            )
        else:
            fund_gate = (not params["fundamental_pass_required"]) or fund_pass
            if (
                signal_score >= params["min_total_score_for_buy"]
                and fund_gate
                and tech_score >= params["min_tech_score_for_buy"]
            ):
                action = "BUY"
            elif signal_score >= 50:
                action = "WATCH"
            else:
                action = "SKIP"

        entry = float(latest["close"])
        stop_price = round(entry * (1 - params["stop_loss"]), 2)
        target_price = round(entry * (1 + params["target_return"]), 2)
        rr = round(params["target_return"] / params["stop_loss"], 2)
        position_pct = min(2.0 / (params["stop_loss"] * 100) * 100, 20.0)
        entry_rule = (
            f"明日以開盤價進場，停損 -{params['stop_loss']*100:.0f}% / "
            f"停利 +{params['target_return']*100:.0f}%（下方參考價為今日收盤）"
        )

        if bt.get("samples", 0) < 8:
            result["risk_notes"].append(f"回測樣本僅 {bt.get('samples', 0)} 次，統計弱")
        if not fund_pass:
            result["risk_notes"].append("基本面未過門檻")
        if winrate < 0.5:
            result["risk_notes"].append(f"歷史勝率 {winrate*100:.0f}% 低於五成")
        if pd.notna(latest.get("bb_upper")) and latest["close"] > latest["bb_upper"]:
            result["risk_notes"].append("已突破布林上軌，追高風險")
        if "放量滯漲" in vp["patterns"]:
            result["risk_notes"].append("偵測到放量滯漲，高檔爆量疑似出貨")
        for w in kl["warnings"]:
            result["risk_notes"].append(w)

        chg_5d = (latest["close"] / px.iloc[-6]["close"] - 1) * 100 if len(px) >= 6 else 0
        chg_20d = (latest["close"] / px.iloc[-21]["close"] - 1) * 100 if len(px) >= 21 else 0
        vol_5 = px["volume"].iloc[-5:].mean()
        vol_20 = px["volume"].iloc[-20:].mean()
        vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1
        high_252 = px["high"].iloc[-252:].max() if len(px) >= 252 else px["high"].max()
        low_252 = px["low"].iloc[-252:].min() if len(px) >= 252 else px["low"].min()
        pct_from_high = (latest["close"] / high_252 - 1) * 100
        above_ma20 = latest["close"] > latest["ma20"] if pd.notna(latest["ma20"]) else False
        above_ma60 = latest["close"] > latest["ma60"] if pd.notna(latest["ma60"]) else False

        result.update({
            "action": action,
            "signal_score": signal_score,
            "components": {
                "fundamental_pass": fund_pass,
                "eps_min": min(eps_vals) if eps_vals else None,
                "roe_min": min(roe_vals) if roe_vals else None,
                "tech_score": tech_score,
                "tech_signals": ts["signals"] + kl["signals"],
                "backtest_winrate": winrate,
                "backtest_samples": bt.get("samples", 0),
                "volume_patterns": vp["patterns"],
                "volume_details": vp["details"],
                "volume_bonus": vp["bonus"],
                "volume_verdict": volume_verdict(vp["patterns"]),
                "kline_domain": kl["domain"],
                "kline_position": kl["position"],
                "kline_verdict": kl["verdict"],
                "kline_warnings": kl["warnings"],
                **({"action_reason": action_reason} if action_mode == "kline_sop" else {}),
                **({"regime": sop_report["regime"]["label"],
                    "regime_hot": sop_report["regime"]["hot"]}
                   if action_mode == "kline_sop" and sop_report and "error" not in sop_report else {}),
            },
            "trend": {
                "chg_5d": round(chg_5d, 2),
                "chg_20d": round(chg_20d, 2),
                "vol_ratio": round(vol_ratio, 2),
                "pct_from_high": round(pct_from_high, 1),
                "above_ma20": bool(above_ma20),
                "above_ma60": bool(above_ma60),
            },
            "entry_price": entry,
            "stop_loss_price": stop_price,
            "target_price": target_price,
            "risk_reward_ratio": rr,
            "position_size_pct": round(position_pct, 1),
            "entry_rule": entry_rule,
        })
        return result

    except Exception as e:
        result["action"] = "ERROR"
        result["risk_notes"].append(f"錯誤: {str(e)[:80]}")
        return result
