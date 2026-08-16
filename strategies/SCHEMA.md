# 策略 JSON Schema

每個策略檔案 = 一個 `.json`，檔名 = `{id}.json`。

## 頂層欄位

| 欄位 | 型別 | 必填 | 說明 |
| --- | --- | --- | --- |
| `id` | string | ✅ | 策略唯一 ID（建議用 slug 或 UUID）|
| `name` | string | ✅ | 顯示名稱 |
| `description` | string |   | 給人看的描述 |
| `source` | `"default" \| "manual" \| "ai"` |   | 來源；前端用來顯示徽章 |
| `created_at` / `updated_at` | ISO 8601 |   | 時間戳 |
| `params` | object | ✅ | 實際策略參數，扁平 dict（見下） |

## `params` 欄位

### 基本面門檻

- `eps_threshold` (number) — 最近兩年 EPS 最小值需 > 此數
- `roe_threshold` (number) — 最近兩年 ROE 最小值需 > 此數 (%)
- `fundamental_pass_required` (bool) — 是否強制基本面要通過才能 BUY

### 回測 & 訊號

- `backtest_years` (int) — 回測年數
- `hold_days` (int) — 持有日數
- `min_tech_score_for_signal` (int 0-100) — 回測時，技術分達多少算一次訊號

### 風險

- `target_return` (0-1) — 停利百分比
- `stop_loss` (0-1) — 停損百分比

### 評分加權

- `weight_fundamental` / `weight_technical` / `weight_backtest` (0-1) — 三者加總應 = 1
- `min_total_score_for_buy` (0-100) — 總分達多少才考慮 BUY
- `min_tech_score_for_buy` (0-100) — 技術分至少要達多少才考慮 BUY

### 技術訊號開關（影響評分）

- `use_ma_alignment` (bool) — 均線多頭排列
- `use_bollinger_bounce` (bool) — 布林下軌反彈
- `use_kd_golden_cross` (bool) — KD 黃金交叉
- `use_macd_bullish` (bool) — MACD 多頭
- `use_volume_patterns` (bool) — 量價型態加減分
- `use_kline_signals` (bool) — 朱家泓K線訊號（收盤價領域＋單/雙/三K＋位置×天數鐵律）加減分，預設 False

### 大盤濾鏡

- `market_filter_enabled` (bool) — 跌破月線時 BUY 自動降 WATCH
- `market_filter_ma_period` (int) — 月線天數
