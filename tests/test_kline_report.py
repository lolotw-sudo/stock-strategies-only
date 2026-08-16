"""kline_report.analyze() 契約測試 + 無未來函數測試。
用南亞科(2408)本機快取資料（.cache/finmind/TaiwanStockPrice__2408.parquet）驗證。
"""

from pathlib import Path

import pandas as pd
import pytest

from stock_strategies.kline_report import analyze

CACHE_FILE = (
    Path(__file__).resolve().parent.parent
    / ".cache"
    / "finmind"
    / "TaiwanStockPrice__2408.parquet"
)


def _load_2408() -> pd.DataFrame:
    df = pd.read_parquet(CACHE_FILE)
    df = df.rename(columns={"max": "high", "min": "low", "Trading_Volume": "volume"})
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)


@pytest.fixture(scope="module")
def df():
    return _load_2408()


TOP_LEVEL_KEYS = {
    "stock_id", "name", "date", "price", "snapshot", "wave", "bottom_stage",
    "rebound_check", "sop", "discipline", "stop_loss", "verdict", "disclaimer",
}


def test_contract_top_level_keys(df):
    r = analyze(df, "2408", "南亞科", -1)
    assert TOP_LEVEL_KEYS <= set(r.keys())


def test_contract_price_block(df):
    r = analyze(df, "2408", "南亞科", -1)
    for k in ["open", "high", "low", "close", "volume", "chg_pct"]:
        assert k in r["price"]
        assert isinstance(r["price"][k], (int, float))


WAVE_TRENDS = {"多頭", "多頭轉弱", "空頭", "打底突破中", "盤整", "盤整偏多", "盤整偏空"}


def test_contract_wave_block(df):
    r = analyze(df, "2408", "南亞科", -1)
    w = r["wave"]
    assert w["trend"] in WAVE_TRENDS
    assert isinstance(w["higher_low"], bool)
    assert isinstance(w["higher_high"], bool)
    assert isinstance(w["broke_above_last_high"], bool)
    assert isinstance(w["broke_below_last_low"], bool)
    assert isinstance(w["recent_highs"], list)
    assert isinstance(w["recent_lows"], list)
    assert isinstance(w["evidence"], str) and w["evidence"]


def test_contract_bottom_stage_block(df):
    r = analyze(df, "2408", "南亞科", -1)
    bs = r["bottom_stage"]
    assert 0 <= bs["stage_index"] <= 4
    assert len(bs["checks"]) == 4
    for c in bs["checks"]:
        assert set(c.keys()) == {"name", "pass", "detail"}
        assert isinstance(c["pass"], bool)
    assert isinstance(bs["skipped_stages"], list)
    assert isinstance(bs["stage_note"], str)


def test_contract_rebound_check_block(df):
    r = analyze(df, "2408", "南亞科", -1)
    rc = r["rebound_check"]
    assert rc["total"] == 4
    assert 0 <= rc["passed"] <= 4
    assert len(rc["items"]) == 4
    for it in rc["items"]:
        assert set(it.keys()) == {"q", "pass", "detail"}


def test_contract_sop_block(df):
    r = analyze(df, "2408", "南亞科", -1)
    sop = r["sop"]
    assert len(sop) == 7
    assert [s["step"] for s in sop] == [1, 2, 3, 4, 5, 6, 7]
    for s in sop:
        assert set(s.keys()) == {"step", "name", "verdict", "detail"}


def test_contract_discipline_block(df):
    r = analyze(df, "2408", "南亞科", -1)
    d = r["discipline"]
    assert d["total"] == 5
    assert 0 <= d["passed"] <= 5
    assert len(d["items"]) == 5
    for it in d["items"]:
        assert set(it.keys()) == {"rule", "pass", "detail"}


def test_contract_stop_loss_block(df):
    r = analyze(df, "2408", "南亞科", -1)
    sl = r["stop_loss"]
    for k in ["recommended", "price", "distance_pct", "reason", "alternatives"]:
        assert k in sl
    assert len(sl["alternatives"]) == 5
    for alt in sl["alternatives"]:
        assert set(alt.keys()) == {"method", "price", "distance_pct"}


def test_insufficient_rows_returns_error():
    small = pd.DataFrame({
        "open": [1.0] * 30, "high": [1.0] * 30, "low": [1.0] * 30,
        "close": [1.0] * 30, "volume": [1.0] * 30,
        "date": pd.date_range("2026-01-01", periods=30),
    })
    r = analyze(small, "0000", "測試", -1)
    assert r == {"error": "資料不足"}


def test_no_future_function(df):
    """鐵律驗證：對 idx 分析的結果，與把 df 截斷到該 idx 後再分析，必須完全相同。"""
    for date in ["2026-08-06", "2026-08-12", "2026-08-13"]:
        idx = df.index[df["date"] == date][0]
        r_full = analyze(df, "2408", "南亞科", idx)
        truncated = df.iloc[: idx + 1].reset_index(drop=True)
        r_truncated = analyze(truncated, "2408", "南亞科", -1)
        assert r_full == r_truncated, f"未來函數洩漏於 {date}"


def test_2408_0812_has_false_breakout_warning(df):
    idx = df.index[df["date"] == "2026-08-12"][0]
    r = analyze(df, "2408", "南亞科", idx)
    assert any("假突破" in w for w in r["snapshot"]["warnings"])


def test_2408_0813_discipline_high_no_chase_and_bias_hot(df):
    idx = df.index[df["date"] == "2026-08-13"][0]
    r = analyze(df, "2408", "南亞科", idx)
    items = {it["rule"]: it for it in r["discipline"]["items"]}
    assert items["高檔不追多"]["pass"] is False
    assert items["乖離未過熱"]["pass"] is False


def test_2408_0813_wave_breaks_above_last_high_is_bottoming_breakout(df):
    """0813 收盤514遠高於最近確認轉折高452（07/23），
    雖然 pivot 結構仍是頭頭低、底底低，但當前價格已突破前高，
    trend 不應再判純空頭，而應標記為「打底突破中」。"""
    idx = df.index[df["date"] == "2026-08-13"][0]
    r = analyze(df, "2408", "南亞科", idx)
    w = r["wave"]
    assert w["trend"] == "打底突破中"
    assert w["broke_above_last_high"] is True
    assert "突破" in w["evidence"]
    # 連鎖修正：純空頭才不過的「不接下跌刀」，在打底突破中不應被誤判為不過
    items = {it["rule"]: it for it in r["discipline"]["items"]}
    assert items["不接下跌刀"]["pass"] is True


def test_2408_0814_skipped_stages_consistent_with_check_pass(df):
    """2408 在 2026-08-14（已從322反彈突破前高452，但②打底的底底高未成立、
    ③突破量能未達1.5x門檻、④trend未達多頭）：skipped_stages 必須與各 check 的
    pass 布林、以及 stage_index 的定義完全一致（不硬寫死哪個階段編號在裡面）。"""
    idx = df.index[df["date"] == "2026-08-14"][0]
    r = analyze(df, "2408", "南亞科", idx)
    bs = r["bottom_stage"]
    checks = bs["checks"]
    expected_skipped = [
        i + 1 for i, c in enumerate(checks) if c["pass"] and (i + 1) > bs["stage_index"]
    ]
    assert bs["skipped_stages"] == expected_skipped
    if bs["skipped_stages"]:
        assert bs["stage_note"] != ""
    else:
        assert bs["stage_note"] == ""
    # ③突破 detail 必須明講量能未達門檻，且點出價格部分已達標（缺陷B驗收）
    stage3 = next(c for c in checks if c["name"] == "③突破")
    assert stage3["pass"] is False
    assert "量能" in stage3["detail"]
    assert "價格部分已達標" in stage3["detail"]


def test_v_shape_reversal_stage_note_and_conservative_stage_index():
    """人造V型反轉資料：①止跌通過，②打底因底底高未成立（V型無二次探底）而不過，
    ③突破的價格與量能條件皆已成立、④因trend尚未達多頭而不過。
    驗證：stage_index 仍保守卡在①（不可跳關），但 skipped_stages 非空，
    且 stage_note 有內容說明後面階段條件已成立。"""
    import numpy as np

    n = 90
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    close = np.zeros(n)
    for i in range(16):
        close[i] = 100 + (10 - abs(i - 10)) * 1.0
    close[10] = 110
    for i in range(16, 56):
        t = (i - 16) / (55 - 16)
        close[i] = 100 - t * 60 + (2 if i % 7 == 0 else 0)
    close[55] = 40
    for i in range(56, 90):
        t = (i - 56) / (89 - 56)
        close[i] = 40 + t * 90
    open_ = close.copy()
    high = close + 1.0
    low = close - 1.0
    volume = np.full(n, 1000.0)
    volume[85:89] = 900.0
    volume[89] = 2500.0  # 突破日爆量，達前5日均量1.5x以上
    df = pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low,
        "close": close, "volume": volume,
    })
    r = analyze(df, "0000", "V型測試", -1)
    bs = r["bottom_stage"]

    checks = {c["name"]: c for c in bs["checks"]}
    assert checks["①止跌"]["pass"] is True
    assert checks["②打底"]["pass"] is False  # 底底高未成立（保守判定卡關處）
    assert checks["③突破"]["pass"] is True  # 條件已成立但因②未過而未被連續計入

    assert bs["stage_index"] == 1
    assert bs["stage"] == "①止跌"
    assert 3 in bs["skipped_stages"]
    assert bs["stage_note"] != ""
    assert "不可跳關" in bs["stage_note"]


def test_2408_pure_downtrend_without_breakout_still_bearish():
    """人造資料：確認pivot為頭頭低、底底低，且當日收盤仍在最近轉折高之下 → 應維持「空頭」。"""
    import numpy as np

    n = 100
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    # 建構一段持續探底、每個反彈都比前一段低的走勢，且今日收盤未突破任何前高
    base = np.linspace(100, 40, n)
    noise = np.tile([0, 2, -2, 1, -1, 3, -3, 0.5, -0.5, 1.5], n // 10 + 1)[:n]
    close = base + noise
    high = close + 1.0
    low = close - 1.0
    open_ = close
    volume = np.full(n, 1000.0)
    df = pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low,
        "close": close, "volume": volume,
    })
    r = analyze(df, "0000", "測試空頭", -1)
    w = r["wave"]
    assert w["broke_above_last_high"] is False
    assert w["trend"] == "空頭"
