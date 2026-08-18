# 📊 Stock Strategies Web UI

Next.js + Tailwind 前端，後端是 `api/` 下的 FastAPI。

## 開發啟動

```bash
# 1. 後端（在專案根目錄）
uv sync                              # 安裝新增的 fastapi / pydantic / google-generativeai
# --reload-include 只看 Python，避免存策略 JSON 時 server 重啟把 /api/run 切斷
uv run uvicorn api.main:app --reload --reload-include '*.py' --port 8010

# 2. 前端（另開一個 terminal，進 web/）
cd web
cp .env.local.example .env.local     # 讓前端直連 FastAPI，避開 Next dev proxy 對長請求的 ECONNRESET
npm install
npm run dev -- -p 3010
```

> **為何要設 `NEXT_PUBLIC_API_BASE`？** Next.js dev server 的 rewrite proxy 對長請求（例如 `/api/run` 跑十幾檔 + FinMind delay）容易出現 `socket hang up / ECONNRESET`。設這個變數後，瀏覽器會直接打 `http://localhost:8010`，FastAPI 的 CORS 已對 `localhost:3010` 開好。

開瀏覽器 http://localhost:3010 即可。

## 環境變數

延用根目錄的 `.env`（FINMIND_TOKEN、GOOGLE_*、TELEGRAM_*），另外新增：

| 變數 | 必填 | 說明 |
| --- | --- | --- |
| `GEMINI_API_KEY` | AI 頁要用 | Google AI Studio 申請 |
| `GEMINI_MODEL` |  | 預設 `gemini-2.5-flash` |
| `CORS_ORIGINS` |  | 預設 `http://localhost:3010,http://localhost:3000` |
| `STRATEGY_DIR` |  | 策略 JSON 存放目錄，預設 `./strategies` |
| `NEXT_PUBLIC_API_BASE` |  | 前端打 API 的 base，預設 `http://localhost:8010` |

## 頁面

- `/` — Dashboard：選策略 + 執行今日選股
- `/strategies` — 策略庫列表
- `/strategies/new` — 手動建立策略（表單）
- `/strategies/ai` — AI 自然語言 → 策略 JSON
- `/strategies/[id]` — 策略詳情 + 跑一次 watchlist

## 資料夾

```
web/
├── app/
│   ├── layout.tsx          全站 layout / 導航
│   ├── page.tsx            Dashboard
│   ├── globals.css
│   └── strategies/
│       ├── page.tsx        列表
│       ├── new/page.tsx    手動建立
│       ├── ai/page.tsx     AI 生
│       └── [id]/page.tsx   詳情
├── components/
│   ├── ActionBadge.tsx
│   └── StrategyForm.tsx
├── lib/api.ts              fetch wrapper
├── tailwind.config.ts
├── next.config.js          /api/* 代理到 FastAPI
└── package.json
```
