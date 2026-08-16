"""read_watchlist() 前導零 bug 的迴歸測試。

gspread 的 ws.get_all_records() 會把看起來像數字的儲存格自動轉成 int/float，
導致 '0050' 變成 50、前導零遺失。sheet.py 改用 ws.get_all_values() + 純函式
parse_watchlist_rows() 手動組裝，全程保持字串，不做任何型別轉換。

這裡直接測純函式 parse_watchlist_rows()（不連真的 Google Sheet），
並用 monkeypatch 驗證 read_watchlist() 對外行為（含 enabled 過濾）不變。
"""
from stock_strategies import sheet


RAW_VALUES = [
    ["stock_id", "name", "enabled"],
    ["0050", "元大台灣50", "TRUE"],
    ["2330", "台積電", "TRUE"],
    ["00631L", "元大台灣50正2", "TRUE"],
    ["", "", ""],
    ["2454", "聯發科", "FALSE"],
]


def test_parse_watchlist_rows_keeps_leading_zero():
    rows = sheet.parse_watchlist_rows(RAW_VALUES)
    stock_ids = [r["stock_id"] for r in rows]
    assert "0050" in stock_ids
    assert "00631L" in stock_ids


def test_parse_watchlist_rows_all_stock_ids_are_str():
    rows = sheet.parse_watchlist_rows(RAW_VALUES)
    assert all(isinstance(r["stock_id"], str) for r in rows)


def test_parse_watchlist_rows_skips_blank_row():
    rows = sheet.parse_watchlist_rows(RAW_VALUES)
    # 空白列（stock_id 為空字串）不應出現在結果中
    assert len(rows) == 4
    assert "" not in [r["stock_id"] for r in rows]


def test_parse_watchlist_rows_empty_input():
    assert sheet.parse_watchlist_rows([]) == []


def test_parse_watchlist_rows_missing_stock_id_header():
    assert sheet.parse_watchlist_rows([["foo", "bar"], ["1", "2"]]) == []


class _FakeWorksheet:
    def __init__(self, values):
        self._values = values

    def get_all_values(self):
        return self._values


class _FakeSheet:
    def __init__(self, values):
        self._values = values

    def worksheet(self, name):
        assert name == "Watchlist"
        return _FakeWorksheet(self._values)


def test_read_watchlist_filters_enabled_and_keeps_leading_zero(monkeypatch):
    monkeypatch.setattr(sheet, "get_gsheet", lambda: _FakeSheet(RAW_VALUES))
    result = sheet.read_watchlist()

    stock_ids = [r["stock_id"] for r in result]
    assert stock_ids == ["0050", "2330", "00631L"]  # FALSE(2454) 與空白列都被排除
    assert all(isinstance(sid, str) for sid in stock_ids)
    assert "0050" in stock_ids
