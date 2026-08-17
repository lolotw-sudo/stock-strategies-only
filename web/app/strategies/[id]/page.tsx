"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, Strategy, RunResult } from "@/lib/api";
import { ActionBadge, SourceBadge } from "@/components/ActionBadge";

export default function StrategyDetail() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const [strategy, setStrategy] = useState<Strategy | null>(null);
  const [running, setRunning] = useState(false);
  const [run, setRun] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getStrategy(id).then(setStrategy).catch((e) => setError(e.message));
  }, [id]);

  async function doRun() {
    setRunning(true);
    setError(null);
    try {
      const r = await api.run(id);
      setRun(r);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setRunning(false);
    }
  }

  if (!strategy && !error) return <div className="card text-muted">載入中…</div>;
  if (error) return <div className="card border-err/40 text-err">錯誤：{error}</div>;
  if (!strategy) return null;

  return (
    <div className="space-y-6">
      <Link href="/strategies" className="text-sm text-muted hover:text-text">← 回策略庫</Link>

      <div className="card">
        <div className="flex items-center gap-2 mb-2">
          <h1 className="text-2xl font-semibold">{strategy.name}</h1>
          <SourceBadge source={strategy.source} />
        </div>
        <div className="text-xs text-muted font-mono mb-3">{strategy.id}</div>
        {strategy.description && <p className="text-sm text-muted leading-relaxed">{strategy.description}</p>}
        <button onClick={doRun} disabled={running} className="btn-primary mt-5">
          {running ? "執行中…" : "▶ 用此策略跑一次 watchlist"}
        </button>
      </div>

      <div className="card">
        <h2 className="font-medium mb-4">策略參數</h2>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-x-6 gap-y-3 text-sm">
          {Object.entries(strategy.params).map(([k, v]) => (
            <div key={k}>
              <div className="text-xs text-muted">{k}</div>
              <div className="font-mono">{renderVal(v)}</div>
            </div>
          ))}
        </div>
      </div>

      {run && (
        <div className="card">
          <h2 className="font-medium mb-3">執行結果</h2>
          <div className="text-sm text-muted mb-3">{run.market.note}</div>
          <div className="flex gap-2 mb-4">
            {run.summary.sell > 0 && <span className="badge-sell">SELL {run.summary.sell}</span>}
            <span className="badge-buy">BUY {run.summary.buy}</span>
            <span className="badge-watch">WATCH {run.summary.watch}</span>
            <span className="badge-skip">SKIP {run.summary.skip}</span>
            {run.summary.error > 0 && <span className="badge-err">ERR {run.summary.error}</span>}
          </div>
          <div className="overflow-x-auto -mx-5">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted">
                <tr className="border-b border-line">
                  <th className="text-left px-5 py-2">代號 / 名稱</th>
                  <th className="text-left px-2 py-2">動作</th>
                  <th className="text-right px-2 py-2">總分</th>
                  <th className="text-right px-2 py-2">技術</th>
                  <th className="text-right px-2 py-2">回測勝率</th>
                  <th className="text-right px-5 py-2">參考價</th>
                </tr>
              </thead>
              <tbody>
                {run.results.map((r) => (
                  <tr key={r.stock_id} className="border-b border-line/50 hover:bg-panel2/50">
                    <td className="px-5 py-2">
                      <div className="font-mono">{r.stock_id}</div>
                      <div className="text-xs text-muted">{r.name}</div>
                    </td>
                    <td className="px-2 py-2"><ActionBadge action={r.action} /></td>
                    <td className="px-2 py-2 text-right font-mono">{r.signal_score ?? "—"}</td>
                    <td className="px-2 py-2 text-right font-mono">{r.components?.tech_score ?? "—"}</td>
                    <td className="px-2 py-2 text-right font-mono">
                      {r.components?.backtest_winrate != null
                        ? `${(r.components.backtest_winrate * 100).toFixed(0)}%`
                        : "—"}
                    </td>
                    <td className="px-5 py-2 text-right font-mono">{r.entry_price ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function renderVal(v: any) {
  if (typeof v === "boolean") return v ? "✓" : "✗";
  if (typeof v === "number") {
    if (v > 0 && v < 1) return v.toFixed(3);
    return String(v);
  }
  return String(v);
}
