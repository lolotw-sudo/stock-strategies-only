"""第十章（參數規格章）回歸測試。

鎖定手冊 10-6 的案例：台達電(2308) 2026-09-01。這一天人工判讀與舊程式邏輯
結論相反——舊邏輯輸出「拉回買點成立，可考慮進場」，隔日 9/02 收黑 −7.24%。
第十章就是為了消滅這個歧異而寫的，所以它必須被測試釘住。
"""

from pathlib import Path

import pandas as pd
import pytest

from stock_strategies.kline_report import analyze

CACHE_FILE = (
    Path(__file__).resolve().parent.parent / ".cache" / "finmind" / "TaiwanStockPrice__2308.parquet"
)

pytestmark = pytest.mark.skipif(not CACHE_FILE.exists(), reason="缺 2308 本機快取")


@pytest.fixture(scope="module")
def df():
    d = pd.read_parquet(CACHE_FILE)
    d = d.rename(columns={"max": "high", "min": "low", "Trading_Volume": "volume"})
    for col in ["open", "high", "low", "close", "volume"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["date"] = pd.to_datetime(d["date"])
    # 與 data.get_price_history 相同：FinMind 會對同一天回傳兩筆，重複會讓均線位移
    d = d.drop_duplicates(subset="date", keep="last")
    return d.sort_values("date").reset_index(drop=True)


@pytest.fixture(scope="module")
def r0901(df):
    return analyze(df, "2308", "台達電", idx=int(df.index[df["date"] == "2026-09-01"][0]))


def test_no_duplicate_dates(df):
    assert int(df["date"].duplicated().sum()) == 0


def test_signal_is_valid(r0901):
    """10-5 做多訊號＝收盤突破 MA5 且 突破前一日最高點。"""
    assert r0901["chapter10"]["signal"]["long"] is True


def test_structure_is_not_bullish(r0901):
    """10-1／10-2：60 日窗口要看得到 8/17 的 2005，否則頭頭低會被誤判成頭頭高。"""
    ch = r0901["chapter10"]["structure"]
    assert ch["qualified"] == "收斂／未表態"
    assert ch["can_long"] is False
    assert 2005.0 in [h["price"] for h in ch["highs"]]
    assert ch["major"]["pass"] is False          # 月線 2585→2520→2135→2005 一路墊低


def test_all_four_filters_fail(r0901):
    """10-6 表：四項濾網全數不通過。"""
    ch = r0901["chapter10"]
    assert ch["passed"] == 0
    assert ch["total"] == 4


def test_risk_reward_matches_manual(r0901):
    """10-4：第一道壓力＝季線 1879，(1879−1865)÷(1865−1810)＝0.25。"""
    ch = r0901["chapter10"]
    assert ch["resistance"] == {"price": 1879.0, "label": "季線"}
    rr = next(f for f in ch["filters"] if f["name"] == "風報比")
    assert rr["value"] == pytest.approx(0.25, abs=0.01)


def test_initial_stop_is_signal_bar_low(r0901):
    """10-3①：初始停損唯一方法是訊號K最低點，不是前波低點法。"""
    stop = r0901["chapter10"]["stop"]
    assert stop["price"] == 1810.0
    assert stop["risk_pct"] == pytest.approx(2.95, abs=0.01)
    assert stop["void"] is False                 # 2.95% 未超過 5% 上限


def test_conclusion_matches_manual(r0901):
    """10-5 措辭禁令：濾網未全過，不得輸出「可考慮進場」。"""
    ch = r0901["chapter10"]
    assert ch["conclusion"] == "訊號成立但不進場，等收盤站上季線 1879"
    assert r0901["plan"]["stance"] == ch["conclusion"]
    assert "可考慮進場" not in r0901["verdict"]


def test_next_day_has_no_signal(df):
    """10-6 B：9/02 長黑跌破 MA5／MA10／MA20，訊號不成立。"""
    r = analyze(df, "2308", "台達電", idx=int(df.index[df["date"] == "2026-09-02"][0]))
    assert r["price"]["close"] == 1730.0
    assert r["chapter10"]["signal"]["long"] is False
    assert r["chapter10"]["conclusion"] == "無進場訊號"
