"""action_mode（score / kline_sop）與 SELL 燈號測試。全程 mock 資料，不打真實網路。

涵蓋：
- loader.validate_strategy / merge_params 對 action_mode 的驗證與預設值
- evaluate() 在 action_mode="score" 時的判定邏輯與改動前一致（迴歸底線）
- _decide_kline_sop_action() 依手冊優先序判定 SELL/BUY/WATCH/SKIP
- evaluate() 在 action_mode="kline_sop" 時的完整管線接線（不斷網跑一次）
- SELL 在排序中最優先
"""
from pathlib import Path

import pandas as pd
import pytest

from stock_strategies import evaluate as evaluate_mod
from stock_strategies.evaluate import evaluate, _decide_kline_sop_action
from stock_strategies.loader import get_strategy, merge_params, validate_strategy

ROOT = Path(__file__).resolve().parent.parent


def make_price_df(n=150, start="2023-01-02", base=100.0):
    """造一段緩步上升的日 K，欄位符合 evaluate() 契約（與 conftest 的 make_price_df 同構）。"""
    dates = pd.bdate_range(start=start, periods=n)
    close = [base + i * 0.3 for i in range(n)]
    return pd.DataFrame({
        "date": dates,
        "open": [c - 0.3 for c in close],
        "high": [c + 0.6 for c in close],
        "low": [c - 0.6 for c in close],
        "close": close,
        "volume": [1000 + i for i in range(n)],
    })


GOOD_FUND = {
    "eps": {2022: 6.0, 2023: 7.0, 2024: 8.0},
    "roe": {2022: 18.0, 2023: 19.0, 2024: 20.0},
}


def _patch_price(monkeypatch, df):
    monkeypatch.setattr(evaluate_mod, "get_price_history", lambda *a, **k: df.copy())


def _patch_fund(monkeypatch, fund=None):
    monkeypatch.setattr(evaluate_mod, "get_fundamental", lambda *a, **k: dict(fund or GOOD_FUND))


# ───────────────────── loader：action_mode 驗證與預設值 ─────────────────────


def test_validate_strategy_invalid_action_mode_falls_back_to_score():
    clean = validate_strategy({"name": "測試非法模式", "params": {"action_mode": "not-a-real-mode"}})
    assert clean["params"]["action_mode"] == "score"


def test_validate_strategy_valid_kline_sop_kept():
    clean = validate_strategy({"name": "測試kline_sop", "params": {"action_mode": "kline_sop"}})
    assert clean["params"]["action_mode"] == "kline_sop"


def test_validate_strategy_missing_action_mode_defaults_to_score():
    clean = validate_strategy({"name": "測試缺欄位"})
    assert clean["params"]["action_mode"] == "score"


def test_merge_params_default_action_mode_is_score():
    assert merge_params(None)["action_mode"] == "score"


def test_kline_chu_strategy_file_uses_kline_sop():
    s = get_strategy("kline-chu")
    assert s is not None
    assert s["params"]["action_mode"] == "kline_sop"


def test_default_strategy_file_unaffected():
    s = get_strategy("default")
    if s is not None:
        # 沒特別設定就該回退 score（迴歸底線：預設策略行為不變）
        assert merge_params(s)["action_mode"] == "score"


# ───────────────────── action_mode="score"：迴歸不變 ─────────────────────


def test_score_mode_action_matches_original_threshold_logic(monkeypatch):
    """action_mode=score（含未指定→預設）時，action 判定邏輯必須與改動前完全一致：
    以回傳的 components 重新套用原始的三段式門檻公式，結果必須吻合。"""
    df = make_price_df(150)
    _patch_price(monkeypatch, df)
    _patch_fund(monkeypatch)

    r = evaluate("2330", "台積電")  # 不給 strategy → action_mode 預設 "score"
    params = merge_params(None)
    c = r["components"]

    fund_gate = (not params["fundamental_pass_required"]) or c["fundamental_pass"]
    if (
        r["signal_score"] >= params["min_total_score_for_buy"]
        and fund_gate
        and c["tech_score"] >= params["min_tech_score_for_buy"]
    ):
        expected = "BUY"
    elif r["signal_score"] >= 50:
        expected = "WATCH"
    else:
        expected = "SKIP"

    assert r["action"] == expected
    assert "action_reason" not in c  # score 模式不應新增這個 key，輸出形狀完全不變


def test_score_mode_explicit_matches_default_omitted(monkeypatch):
    df = make_price_df(150)
    _patch_price(monkeypatch, df)
    _patch_fund(monkeypatch)

    r1 = evaluate("2330", "台積電")
    r2 = evaluate("2330", "台積電", strategy={"id": "x", "params": {"action_mode": "score"}})
    assert r1["action"] == r2["action"]
    assert r1["signal_score"] == r2["signal_score"]


def test_invalid_action_mode_in_raw_strategy_falls_back_to_score(monkeypatch):
    """evaluate() 自己也要防禦：merge_params 不做驗證，未經 validate_strategy 洗過的
    策略 dict 若帶非法 action_mode，evaluate() 仍要安全回退為 score。"""
    df = make_price_df(150)
    _patch_price(monkeypatch, df)
    _patch_fund(monkeypatch)

    r = evaluate("2330", "台積電", strategy={"id": "x", "params": {"action_mode": "bogus"}})
    assert "action_reason" not in r["components"]


# ───────────────────── kline_sop：_decide_kline_sop_action 優先序單元測試 ─────────────────────


def _disc(passed, total, failing_rule=None):
    rules = ["高檔不追多", "乖離未過熱", "突破需收盤確認", "長線保護短線", "不接下跌刀"]
    items = []
    for r in rules:
        ok = not (failing_rule and r == failing_rule)
        items.append({"rule": r, "pass": ok, "detail": "ok" if ok else "未達標細節"})
    return {"passed": passed, "total": total, "items": items}


def _sop(rebound_passed=0, disc=None, *, code="basing", label="打底中", hot=False,
         bias=0.0, cl_passed=None, cl_total=4, cl_name="5-2-0 搶反彈四問"):
    """造一份 _decide_kline_sop_action() 需要的 sop_report。

    預設為「打底中」狀態（走搶反彈四問），與改動前的行為對應——原有測試沿用此預設，
    斷言不變，藉此確認狀態分流沒有改變舊路徑的判定結果。
    """
    rc = {"passed": rebound_passed, "total": 4}
    cl_passed = rebound_passed if cl_passed is None else cl_passed
    items = [{"rule": f"項目{i+1}", "pass": i < cl_passed, "detail": "細節"} for i in range(cl_total)]
    return {
        "rebound_check": rc,
        "discipline": disc if disc is not None else _disc(5, 5),
        "regime": {"code": code, "label": label, "hot": hot, "bias_ma20": bias,
                   "reason": f"波浪型態X、打底階段Y"},
        "checklist": None if code == "downtrend" else
                     {"name": cl_name, "passed": cl_passed, "total": cl_total, "items": items},
    }


def test_sell_new_cross_below_ma20():
    """昨日收盤 ≥ MA20、今日收盤 < MA20 → SELL（最高優先）。"""
    px = pd.DataFrame({"close": [130.0, 130.0, 100.0], "ma20": [125.0, 126.0, 125.5]})
    latest = px.iloc[-1]
    risk_notes = []
    sop_report = _sop(0, _disc(0, 5))

    action, reason = _decide_kline_sop_action(px, latest, sop_report, risk_notes)

    assert action == "SELL"
    assert any("跌破月線" in n for n in risk_notes)
    assert "跌破月線" in reason


def test_already_below_ma20_multiple_days_is_not_sell():
    """已連續多日在月線下方（非新跌破）→ 不給 SELL，但 risk_notes 要註記天數。"""
    px = pd.DataFrame({"close": [100.0, 98.0, 97.0], "ma20": [125.0, 120.0, 115.0]})
    latest = px.iloc[-1]
    risk_notes = []
    sop_report = _sop(0, _disc(0, 5))

    action, _ = _decide_kline_sop_action(px, latest, sop_report, risk_notes)

    assert action != "SELL"
    assert any("已在月線下方" in n for n in risk_notes)
    assert any("3 日" in n for n in risk_notes if "已在月線下方" in n)


def test_buy_when_rebound_full_discipline_full_and_above_ma20():
    """四問4/4 + 紀律全過 + 站上月線 → BUY。"""
    px = pd.DataFrame({"close": [100.0, 101.0, 102.0], "ma20": [95.0, 96.0, 97.0]})
    latest = px.iloc[-1]
    risk_notes = []
    sop_report = _sop(4, _disc(5, 5))

    action, reason = _decide_kline_sop_action(px, latest, sop_report, risk_notes)

    assert action == "BUY"
    assert "站上月線" in reason
    assert not any("跌破月線" in n for n in risk_notes)


def test_watch_when_rebound_full_but_discipline_fails():
    """四問4/4 但紀律有未過項 → WATCH，risk_notes 含「紀律未過」。"""
    px = pd.DataFrame({"close": [100.0, 101.0, 102.0], "ma20": [95.0, 96.0, 97.0]})
    latest = px.iloc[-1]
    risk_notes = []
    sop_report = _sop(4, _disc(4, 5, failing_rule="乖離未過熱"))

    action, reason = _decide_kline_sop_action(px, latest, sop_report, risk_notes)

    assert action == "WATCH"
    assert any("紀律未過" in n for n in risk_notes)
    assert "未全過" in reason


def test_watch_when_rebound_passed_three():
    px = pd.DataFrame({"close": [100.0, 101.0, 102.0], "ma20": [95.0, 96.0, 97.0]})
    latest = px.iloc[-1]
    risk_notes = []
    sop_report = _sop(3, _disc(5, 5))

    action, reason = _decide_kline_sop_action(px, latest, sop_report, risk_notes)
    assert action == "WATCH"
    assert "3/4" in reason


def test_skip_when_rebound_passed_two_or_less():
    px = pd.DataFrame({"close": [100.0, 101.0, 102.0], "ma20": [95.0, 96.0, 97.0]})
    latest = px.iloc[-1]
    risk_notes = []
    sop_report = _sop(2, _disc(5, 5))

    action, reason = _decide_kline_sop_action(px, latest, sop_report, risk_notes)
    assert action == "SKIP"
    assert "2/4" in reason


def test_sell_overrides_even_if_rebound_would_qualify_for_buy():
    """SELL 優先序最高：即使四問/紀律都達標，只要今日新跌破月線，仍判 SELL。"""
    px = pd.DataFrame({"close": [130.0, 130.0, 100.0], "ma20": [125.0, 126.0, 125.5]})
    latest = px.iloc[-1]
    risk_notes = []
    sop_report = _sop(4, _disc(5, 5))

    action, _ = _decide_kline_sop_action(px, latest, sop_report, risk_notes)
    assert action == "SELL"


# ───────────────────── kline_sop：evaluate() 全管線接線測試 ─────────────────────


def test_evaluate_kline_sop_mode_wires_correctly(monkeypatch):
    df = make_price_df(150)
    _patch_price(monkeypatch, df)
    _patch_fund(monkeypatch)

    strategy = {
        "id": "kline-chu-test",
        "params": {"action_mode": "kline_sop", "fundamental_pass_required": False},
    }
    r = evaluate("2330", "台積電", strategy=strategy)

    assert r["action"] in ("SELL", "BUY", "WATCH", "SKIP")
    assert "action_reason" in r["components"]
    assert isinstance(r["components"]["action_reason"], str) and r["components"]["action_reason"]
    # signal_score 仍照原公式計算並回傳，不因 action_mode 而消失
    assert isinstance(r["signal_score"], float)


# ───────────────────── SELL 排序最優先 ─────────────────────


def test_sort_order_puts_sell_first():
    order = {"SELL": 0, "BUY": 1, "WATCH": 2, "SKIP": 3, "ERROR": 4}
    results = [
        {"action": "BUY", "signal_score": 90},
        {"action": "SKIP", "signal_score": 10},
        {"action": "SELL", "signal_score": 50},
        {"action": "WATCH", "signal_score": 60},
    ]
    results.sort(key=lambda x: (order.get(x.get("action"), 5), -x.get("signal_score", 0)))
    assert results[0]["action"] == "SELL"


def test_main_api_export_board_order_dicts_rank_sell_first():
    for relpath in ("main.py", "api/main.py", "scripts/export_board.py"):
        text = (ROOT / relpath).read_text(encoding="utf-8")
        assert ('"SELL": 0' in text) or ("'SELL': 0" in text), f"{relpath} 排序未見 SELL 排最前"


def test_export_board_order_constant_runtime_values():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "export_board_for_test", ROOT / "scripts" / "export_board.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.ORDER["SELL"] < mod.ORDER["BUY"] < mod.ORDER["WATCH"] < mod.ORDER["SKIP"] < mod.ORDER["ERROR"]


def test_api_main_summary_includes_sell_key():
    text = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    assert '"sell":' in text


def test_export_board_summary_includes_sell_key():
    text = (ROOT / "scripts" / "export_board.py").read_text(encoding="utf-8")
    assert '"sell":' in text


# ───────────────── 狀態分流（regime）：五態 + 過熱旗標 ─────────────────


def _px_above_ma20():
    return pd.DataFrame({"close": [100.0, 101.0, 102.0], "ma20": [95.0, 96.0, 97.0]})


def test_downtrend_always_skip():
    """空頭續跌（手冊第六章 ⚪ 不進場）→ 一律 SKIP，不看任何檢查表。"""
    px = _px_above_ma20()
    risk_notes = []
    sop = _sop(code="downtrend", label="空頭續跌")

    action, reason = _decide_kline_sop_action(px, px.iloc[-1], sop, risk_notes)
    assert action == "SKIP"
    assert "不進場" in reason


def test_hot_flag_blocks_buy_even_when_checklist_full():
    """乖離 >20% 掛過熱旗標 → 即使檢查表與紀律全過也不給 BUY，改 WATCH 並提示停利。"""
    px = _px_above_ma20()
    risk_notes = []
    sop = _sop(4, _disc(5, 5), code="uptrend", label="多頭行進", hot=True, bias=23.4)

    action, reason = _decide_kline_sop_action(px, px.iloc[-1], sop, risk_notes)
    assert action == "WATCH"
    assert "過熱" in reason
    assert any("停利" in n for n in risk_notes)


def test_uptrend_buy_uses_pullback_checklist():
    """多頭行進走 7-1 續漲拉回買點；全過 + 紀律全過 + 站上月線 → BUY。"""
    px = _px_above_ma20()
    risk_notes = []
    sop = _sop(0, _disc(5, 5), code="uptrend", label="多頭行進",
               cl_passed=4, cl_total=4, cl_name="7-1 續漲拉回買點")

    action, reason = _decide_kline_sop_action(px, px.iloc[-1], sop, risk_notes)
    assert action == "BUY"
    assert "多頭行進" in reason and "7-1" in reason


def test_breakout_excludes_high_chase_discipline():
    """創新高狀態下「高檔不追多」由 7-3 的①天數②乖離取代——該項未過仍可 BUY。

    否則以「距60日高點≤5%」為準的高檔判定，會讓創新高這個狀態永遠無法成立。
    """
    px = _px_above_ma20()
    risk_notes = []
    sop = _sop(0, _disc(4, 5, failing_rule="高檔不追多"), code="breakout", label="創新高",
               cl_passed=4, cl_total=4, cl_name="7-3 創新高四關")

    action, reason = _decide_kline_sop_action(px, px.iloc[-1], sop, risk_notes)
    assert action == "BUY"
    assert "7-3" in reason


def test_high_chase_discipline_still_blocks_non_breakout():
    """對照組：同樣是「高檔不追多」未過，非創新高狀態就擋得住（排除只在 breakout 生效）。"""
    px = _px_above_ma20()
    risk_notes = []
    sop = _sop(0, _disc(4, 5, failing_rule="高檔不追多"), code="uptrend", label="多頭行進",
               cl_passed=4, cl_total=4, cl_name="7-1 續漲拉回買點")

    action, _ = _decide_kline_sop_action(px, px.iloc[-1], sop, risk_notes)
    assert action == "WATCH"
    assert any("高檔不追多" in n for n in risk_notes)


def test_sell_overrides_hot_flag():
    """SELL 仍是最高優先：新跌破月線時，過熱旗標不影響判定。"""
    px = pd.DataFrame({"close": [130.0, 130.0, 100.0], "ma20": [125.0, 126.0, 125.5]})
    risk_notes = []
    sop = _sop(4, _disc(5, 5), code="uptrend", label="多頭行進", hot=True, bias=25.0)

    action, _ = _decide_kline_sop_action(px, px.iloc[-1], sop, risk_notes)
    assert action == "SELL"


def test_analyze_emits_regime_and_matching_checklist():
    """analyze() 必須產出 regime，且 checklist 與狀態相符（空頭續跌沒有買點檢查表）。"""
    from stock_strategies.kline_report import analyze

    df = make_price_df(200)
    r = analyze(df, "2330", "台積電")
    assert "regime" in r and r["regime"]["code"] in (
        "breakout", "uptrend", "confirmed", "basing", "downtrend"
    )
    if r["regime"]["code"] == "downtrend":
        assert r["checklist"] is None
    else:
        assert r["checklist"]["total"] >= 4
        assert isinstance(r["checklist"]["name"], str)
