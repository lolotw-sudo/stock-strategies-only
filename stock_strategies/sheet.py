import os
import json

import gspread
from google.oauth2.service_account import Credentials


def get_gsheet():
    creds_json = os.environ["GOOGLE_CREDS_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])


def parse_watchlist_rows(values: list[list[str]]) -> list[dict]:
    """把 ws.get_all_values() 回傳的原始二維字串陣列轉成 list[dict]，不做任何型別轉換。

    刻意不用 gspread 的 get_all_records()：它會把看起來像數字的儲存格自動轉成
    int/float，導致 '0050' 變成 50、前導零遺失。這裡全程保持字串。

    - 第一列是表頭，各欄名稱 strip() 後比對
    - 之後每列的值皆保持原始字串並 strip()
    - stock_id 為空字串的列（空白列）會被跳過
    - 沒有 stock_id 欄位（表頭不符）時回傳空 list
    """
    if not values:
        return []
    headers = [h.strip() for h in values[0]]
    if "stock_id" not in headers:
        return []
    sid_idx = headers.index("stock_id")
    n = len(headers)
    rows = []
    for raw_row in values[1:]:
        padded = list(raw_row) + [""] * (n - len(raw_row))  # Sheet 尾端空欄可能被省略
        stock_id = padded[sid_idx].strip()
        if not stock_id:
            continue
        row = {headers[i]: padded[i].strip() for i in range(n)}
        row["stock_id"] = stock_id
        rows.append(row)
    return rows


def parse_holding(row: dict) -> dict | None:
    """把 Watchlist 的 cost／shares 欄轉成數字。

    cost 是「最高單筆成本」（非平均成本）。cost 空白代表尚在觀望、沒有部位，回傳 None。
    使用者可能把股數填成千分位（例如 42,400），也可能只填成本不填股數，兩者都要容錯。
    """
    def _num(v):
        text = str(v or "").replace(",", "").strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    cost = _num(row.get("cost"))
    if cost is None or cost <= 0:
        return None
    shares = _num(row.get("shares"))
    return {
        "cost": cost,
        "shares": int(shares) if shares and shares > 0 else None,
        "cost_basis": "最高單筆成本（非平均成本）",
    }


def read_watchlist() -> list[dict]:
    """從 Google Sheet Watchlist 分頁讀股票清單（stock_id 保持原始字串，前導零不遺失）"""
    sh = get_gsheet()
    ws = sh.worksheet("Watchlist")
    rows = parse_watchlist_rows(ws.get_all_values())
    enabled = [
        r for r in rows
        if str(r.get("enabled", "")).upper() in ("TRUE", "1", "YES")
    ]
    return enabled


def _watchlist_rows_with_sheet_rownum(values: list[list[str]]) -> list[dict]:
    """把原始二維字串陣列轉成 list[dict]，**不跳過空白列**，用於需要用
    enumerate(rows, start=2) 對應實際 sheet row number 的場景（update_cell 等）。
    stock_id 等值同樣保持字串並 strip()，不做型別轉換。
    """
    if not values:
        return []
    headers = [h.strip() for h in values[0]]
    n = len(headers)
    rows = []
    for raw_row in values[1:]:
        padded = list(raw_row) + [""] * (n - len(raw_row))
        rows.append({headers[i]: padded[i].strip() for i in range(n)})
    return rows


def append_signals(signals: list[dict]):
    """把結果寫回 Signals 分頁"""
    if not signals:
        return
    sh = get_gsheet()
    try:
        ws = sh.worksheet("Signals")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Signals", rows=1000, cols=20)
        ws.append_row([
            "date", "stock_id", "name", "action", "signal_score",
            "entry_price", "stop_loss_price", "target_price",
            "rr_ratio", "position_pct", "winrate", "samples",
            "tech_signals", "risk_notes"
        ])

    rows = []
    for s in signals:
        c = s.get("components", {})
        rows.append([
            s.get("date", ""),
            s.get("stock_id", ""),
            s.get("name", ""),
            s.get("action", ""),
            s.get("signal_score", ""),
            s.get("entry_price", ""),
            s.get("stop_loss_price", ""),
            s.get("target_price", ""),
            s.get("risk_reward_ratio", ""),
            s.get("position_size_pct", ""),
            c.get("backtest_winrate", ""),
            c.get("backtest_samples", ""),
            ", ".join(c.get("tech_signals", [])),
            " / ".join(s.get("risk_notes", [])),
        ])
    ws.append_rows(rows)


def _ensure_watchlist_headers(ws) -> list[str]:
    """讀第一列 headers，沒 headers 就建好 stock_id/name/enabled 三欄。"""
    values = ws.get_all_values()
    if not values:
        headers = ["stock_id", "name", "enabled"]
        ws.append_row(headers)
        return headers
    headers = [h.strip() for h in values[0]]
    if "stock_id" not in headers or "enabled" not in headers:
        # 既有 sheet 有資料但 schema 不符，謹慎處理 — 不擅自改 headers
        return headers
    return headers


def add_to_watchlist(stock_id: str, name: str = "") -> dict:
    """加一檔到 Watchlist 分頁。

    若該 stock_id 已存在但 enabled=FALSE → 直接改回 TRUE（重啟用）
    若已存在且 enabled=TRUE → 不重複加，回傳 status='exists'
    若不存在 → append 新 row（enabled=TRUE）
    """
    sh = get_gsheet()
    ws = sh.worksheet("Watchlist")
    headers = _ensure_watchlist_headers(ws)

    sid_col = headers.index("stock_id") + 1  # gspread 是 1-based
    name_col = headers.index("name") + 1 if "name" in headers else None
    en_col = headers.index("enabled") + 1

    rows = _watchlist_rows_with_sheet_rownum(ws.get_all_values())
    for i, r in enumerate(rows, start=2):  # row 1 是 header
        if str(r.get("stock_id", "")).strip() == str(stock_id).strip():
            current = str(r.get("enabled", "")).upper()
            if current in ("TRUE", "1", "YES"):
                return {
                    "status": "exists",
                    "stock_id": stock_id,
                    "name": r.get("name", name),
                }
            ws.update_cell(i, en_col, "TRUE")
            return {
                "status": "reenabled",
                "stock_id": stock_id,
                "name": r.get("name", name),
            }

    # 不存在 → append
    new_row = [""] * len(headers)
    new_row[sid_col - 1] = str(stock_id)
    if name_col is not None:
        new_row[name_col - 1] = name
    new_row[en_col - 1] = "TRUE"
    ws.append_row(new_row)
    return {"status": "added", "stock_id": stock_id, "name": name}


def remove_from_watchlist(stock_id: str) -> dict:
    """把 Watchlist 該 stock_id 的 enabled 改成 FALSE（軟刪除，保留歷史）"""
    sh = get_gsheet()
    ws = sh.worksheet("Watchlist")
    headers = _ensure_watchlist_headers(ws)
    if "enabled" not in headers:
        return {"status": "no_enabled_column"}
    en_col = headers.index("enabled") + 1

    rows = _watchlist_rows_with_sheet_rownum(ws.get_all_values())
    for i, r in enumerate(rows, start=2):
        if str(r.get("stock_id", "")).strip() == str(stock_id).strip():
            ws.update_cell(i, en_col, "FALSE")
            return {"status": "disabled", "stock_id": stock_id}
    return {"status": "not_found", "stock_id": stock_id}


def read_latest_signals(limit: int = 50) -> list[dict]:
    """從 Signals 分頁讀最近 N 筆紀錄（依 row 順序，最後 N 筆）。

    若該分頁不存在 → 回空 list（代表還沒跑過）
    """
    sh = get_gsheet()
    try:
        ws = sh.worksheet("Signals")
    except gspread.WorksheetNotFound:
        return []
    rows = ws.get_all_records()
    if not rows:
        return []
    return rows[-limit:][::-1]  # 最新的在最前面


PERFORMANCE_HEADERS = [
    "signal_date", "stock_id", "name", "entry_close", "entry_open",
    "t5_date", "t5_close", "t5_ret",
    "t10_date", "t10_close", "t10_ret",
    "t20_date", "t20_close", "t20_ret",
    "hit_target", "hit_stop", "status",
]


def read_performance() -> list[dict]:
    """讀取 Performance 分頁的所有追蹤紀錄（若尚未建立則回空 list）"""
    sh = get_gsheet()
    try:
        ws = sh.worksheet("Performance")
    except gspread.WorksheetNotFound:
        return []
    return ws.get_all_records()


def write_performance(records: list[dict]):
    """整張 Performance 分頁清空重寫（紀錄數不多，效率 OK）"""
    sh = get_gsheet()
    try:
        ws = sh.worksheet("Performance")
        ws.clear()
    except gspread.WorksheetNotFound:
        rows_alloc = max(2000, len(records) + 100)
        ws = sh.add_worksheet(
            title="Performance", rows=rows_alloc, cols=len(PERFORMANCE_HEADERS)
        )

    ws.append_row(PERFORMANCE_HEADERS)
    if not records:
        return

    rows = [[r.get(h, "") for h in PERFORMANCE_HEADERS] for r in records]
    ws.append_rows(rows)
