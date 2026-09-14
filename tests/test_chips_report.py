"""籌碼判讀測試：判讀表必須全覆蓋，缺料時必須降級而不是炸掉。"""
import itertools

import pandas as pd
import pytest

from stock_strategies import chips_report as cr


def test_read_table_covers_every_combination():
    """27 種 (股價,主力,散戶) 組合都要查得到判讀，不能 KeyError。
    主力 flat 的 9 種由 _MAIN_FLAT 依散戶方向收斂成 3 種。"""
    dirs = ["up", "flat", "down"]
    for p, m, r in itertools.product(dirs, repeat=3):
        if m == "flat":
            assert r in cr._MAIN_FLAT
        else:
            assert (p, m, r) in cr._READS, f"判讀表缺 {(p, m, r)}"
    # 每條判讀都是 (code, label, headline) 三元組且非空
    for v in list(cr._READS.values()) + list(cr._MAIN_FLAT.values()):
        assert len(v) == 3 and all(isinstance(x, str) and x for x in v)


@pytest.mark.parametrize("value,th,expected", [
    (5.0, 2.0, "up"), (-5.0, 2.0, "down"), (1.0, 2.0, "flat"),
    (2.0, 2.0, "up"), (-2.0, 2.0, "down"), (0.0, 2.0, "flat"),
])
def test_dir_threshold_is_inclusive(value, th, expected):
    assert cr._dir(value, th) == expected


def test_net_lots_converts_shares_to_lots():
    s = pd.Series([1_000_000, -500_000, 250_000])
    assert cr._net_lots(s, 3) == 750        # 750,000 股 = 750 張
    assert cr._net_lots(s, 1) == 250
    assert cr._net_lots(pd.Series([], dtype=float), 5) == 0.0


def test_empty_price_returns_error():
    assert "error" in cr.analyze_chips("2330", pd.DataFrame())


def _fake_px(n=30, close=100.0):
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="B"),
        "close": [close] * n,
        "volume": [10_000_000] * n,
    })


def test_missing_chip_data_degrades_to_neutral(monkeypatch):
    """三個籌碼 dataset 全空時，不 raise、判讀退回無方向並在 notes 說明。"""
    empty = pd.DataFrame()
    monkeypatch.setattr(cr.ds, "get_institutional", lambda *a, **k: empty)
    monkeypatch.setattr(cr.ds, "get_margin", lambda *a, **k: empty)
    monkeypatch.setattr(cr.ds, "get_shareholding", lambda *a, **k: empty)

    out = cr.analyze_chips("9999", _fake_px())
    assert out["read"]["code"] == "neutral"
    assert out["flows"] == [] and out["retail"] is None
    assert any("法人" in n for n in out["notes"])
    assert out["unavailable"]                      # 券商分點的限制要誠實顯示


def test_washout_combination(monkeypatch):
    """股價跌 + 法人買超 + 融資減 → 洗盤。這是使用者要的核心判讀。"""
    px = _fake_px()
    px.loc[px.index[-1], "close"] = 90.0            # 5 日跌 10%
    dates = px["date"].tail(25)
    inst = pd.DataFrame({
        "date": dates,
        "foreign_net": [500_000] * 25,              # 每日買超 500 張
        "trust_net": [0] * 25, "dealer_net": [0] * 25,
        "total_net": [500_000] * 25,
    })
    margin = pd.DataFrame({
        "date": dates,
        "margin_balance": [10_000 - i * 100 for i in range(25)],   # 融資遞減，5 日約 -6%
        "short_margin_ratio": [0.01] * 25,
    })
    monkeypatch.setattr(cr.ds, "get_institutional", lambda *a, **k: inst)
    monkeypatch.setattr(cr.ds, "get_margin", lambda *a, **k: margin)
    monkeypatch.setattr(cr.ds, "get_shareholding", lambda *a, **k: pd.DataFrame())

    out = cr.analyze_chips("9999", px)
    assert out["read"]["price_5d_pct"] == -10.0
    assert out["read"]["main_5d_lots"] == 2500      # 5 日 × 500 張
    assert out["read"]["code"] == "washout"


def test_distribution_combination(monkeypatch):
    """股價漲 + 法人賣超 + 融資增 → 出貨。"""
    px = _fake_px()
    px.loc[px.index[-1], "close"] = 115.0
    dates = px["date"].tail(25)
    inst = pd.DataFrame({
        "date": dates,
        "foreign_net": [-500_000] * 25, "trust_net": [0] * 25,
        "dealer_net": [0] * 25, "total_net": [-500_000] * 25,
    })
    margin = pd.DataFrame({
        "date": dates,
        "margin_balance": [10_000 + i * 100 for i in range(25)],   # 融資遞增，5 日約 +4%
        "short_margin_ratio": [0.01] * 25,
    })
    monkeypatch.setattr(cr.ds, "get_institutional", lambda *a, **k: inst)
    monkeypatch.setattr(cr.ds, "get_margin", lambda *a, **k: margin)
    monkeypatch.setattr(cr.ds, "get_shareholding", lambda *a, **k: pd.DataFrame())

    assert cr.analyze_chips("9999", px)["read"]["code"] == "distribution"
