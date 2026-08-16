"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

function Check({ pass }: { pass: boolean }) {
  return <span className={pass ? "text-buy" : "text-err"}>{pass ? "✅" : "❌"}</span>;
}

function distLabel(pct: number): string {
  // distance_pct = (現價-停損價)/現價*100：正常為正值（停損在下方）；
  // 若停損法算出的價位已在現價之上（例如收盤已跌破該均線），值會是負的，
  // 代表用該方法「已經跌破」，不能再套用「-」前綴（會變成 --0.4% 的顯示錯誤）。
  return pct >= 0 ? `-${pct}%` : `已跌破（+${Math.abs(pct)}%）`;
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <div className="text-xs uppercase tracking-wider text-muted mb-2">{children}</div>;
}

const STAGE_LABELS = ["未止跌", "①止跌", "②打底", "③突破", "④回升確立"];

export function KlineReport({ stockId, name }: { stockId: string; name: string }) {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);
    api
      .analyze(stockId, name)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e: any) => {
        if (!cancelled) setError(e.message || "分析失敗");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [stockId, name]);

  if (loading) {
    return <div className="bg-panel2 border border-line rounded-lg p-4 text-sm text-muted">K線分析載入中…</div>;
  }
  if (error) {
    return <div className="bg-panel2 border border-line rounded-lg p-4 text-sm text-err">分析失敗：{error}</div>;
  }
  if (!data) return null;

  const { price, snapshot, wave, bottom_stage, rebound_check, sop, discipline, stop_loss, verdict, disclaimer } = data;

  return (
    <div className="bg-panel2 border border-line rounded-lg p-4 space-y-5">
      {/* 頂部：位置＋收盤價領域 */}
      <div>
        <div className="flex items-center gap-3 flex-wrap">
          <span className="badge-watch">{snapshot.position || "—"}第{snapshot.leg_days}天</span>
          <span className="text-sm">
            {data.date} 收盤 <span className="font-mono">{price.close}</span>{" "}
            <span className={price.chg_pct >= 0 ? "text-buy" : "text-err"}>
              {price.chg_pct >= 0 ? "+" : ""}
              {price.chg_pct}%
            </span>
          </span>
          {snapshot.domain?.type && <span className="badge-skip">{snapshot.domain.type}</span>}
        </div>
        {snapshot.domain?.type && (
          <div className="mt-2">
            <div className="flex h-4 rounded overflow-hidden border border-line">
              <div className="bg-buy/60 flex items-center justify-center text-[10px]" style={{ width: `${snapshot.domain.buyer_pct}%` }}>
                {snapshot.domain.buyer_pct > 15 ? `買${snapshot.domain.buyer_pct}%` : ""}
              </div>
              <div className="bg-err/60 flex items-center justify-center text-[10px]" style={{ width: `${snapshot.domain.seller_pct}%` }}>
                {snapshot.domain.seller_pct > 15 ? `賣${snapshot.domain.seller_pct}%` : ""}
              </div>
            </div>
          </div>
        )}
        {snapshot.warnings?.length > 0 && (
          <div className="text-xs text-err mt-2">⚠️ {snapshot.warnings.join("；")}</div>
        )}
      </div>

      {/* 搶反彈四問 */}
      <div>
        <SectionTitle>搶反彈四問（{rebound_check.passed}/{rebound_check.total}）— {rebound_check.conclusion}</SectionTitle>
        <div className="space-y-1">
          {rebound_check.items.map((it: any, i: number) => (
            <div key={i} className="flex items-start gap-2 text-xs">
              <Check pass={it.pass} />
              <span className="w-24 shrink-0 text-muted">{it.q}</span>
              <span>{it.detail}</span>
            </div>
          ))}
        </div>
      </div>

      {/* 底部四階段 */}
      <div>
        <SectionTitle>空頭轉多頭四階段（現況：{bottom_stage.stage}）</SectionTitle>
        {bottom_stage.stage_note && (
          <div className="text-xs text-muted mb-2">{bottom_stage.stage_note}</div>
        )}
        <div className="flex gap-1 mb-2">
          {STAGE_LABELS.slice(1).map((label, i) => {
            const stageNum = i + 1;
            const reached = bottom_stage.stage_index >= stageNum;
            const skipped = !reached && (bottom_stage.skipped_stages || []).includes(stageNum);
            return (
              <div
                key={label}
                title={skipped ? "條件已成立，但因前面階段未完成而未計入（不可跳關）" : undefined}
                className={
                  "flex-1 text-center text-[11px] py-1.5 rounded " +
                  (reached
                    ? "bg-buy/25 text-buy border border-buy/40"
                    : skipped
                    ? "bg-panel/40 text-muted border border-dashed border-line"
                    : "bg-panel text-muted border border-line")
                }
              >
                {label}
              </div>
            );
          })}
        </div>
        <div className="space-y-1">
          {bottom_stage.checks.map((c: any, i: number) => (
            <div key={i} className="flex items-start gap-2 text-xs">
              <Check pass={c.pass} />
              <span className="w-16 shrink-0 text-muted">{c.name}</span>
              <span>{c.detail}</span>
            </div>
          ))}
        </div>
      </div>

      {/* 波浪型態 */}
      <div>
        <SectionTitle>波浪型態</SectionTitle>
        <div className="flex items-center gap-2 mb-1">
          <span className={wave.trend === "多頭" ? "badge-buy" : wave.trend === "空頭" ? "badge-err" : "badge-skip"}>
            {wave.trend}
          </span>
          <span className="text-sm">{wave.pattern}</span>
        </div>
        <div className="text-xs text-muted mb-2">{wave.evidence}</div>
        <div className="grid grid-cols-2 gap-3 text-xs">
          <div>
            <div className="text-muted mb-1">近期轉折高</div>
            {wave.recent_highs.map((p: any, i: number) => (
              <div key={i} className="font-mono">{p.date} {p.price}</div>
            ))}
          </div>
          <div>
            <div className="text-muted mb-1">近期轉折低</div>
            {wave.recent_lows.map((p: any, i: number) => (
              <div key={i} className="font-mono">{p.date} {p.price}</div>
            ))}
          </div>
        </div>
      </div>

      {/* 7步SOP */}
      <div>
        <SectionTitle>實戰SOP（第八章）</SectionTitle>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-muted text-left border-b border-line">
                <th className="py-1 pr-2 w-8">步驟</th>
                <th className="py-1 pr-2">檢查項目</th>
                <th className="py-1 pr-2">判定</th>
                <th className="py-1">說明</th>
              </tr>
            </thead>
            <tbody>
              {sop.map((s: any) => (
                <tr key={s.step} className="border-b border-line/50 align-top">
                  <td className="py-1.5 pr-2 text-muted">{s.step}</td>
                  <td className="py-1.5 pr-2 whitespace-nowrap">{s.name}</td>
                  <td className="py-1.5 pr-2 font-medium whitespace-nowrap">{s.verdict}</td>
                  <td className="py-1.5 text-muted">{s.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* 紀律檢查 */}
      <div>
        <SectionTitle>紀律檢查（{discipline.passed}/{discipline.total}）</SectionTitle>
        <div className="space-y-1">
          {discipline.items.map((it: any, i: number) => (
            <div key={i} className={"flex items-start gap-2 text-xs " + (it.pass ? "" : "text-err")}>
              <Check pass={it.pass} />
              <span className="w-24 shrink-0 text-muted">{it.rule}</span>
              <span>{it.detail}</span>
            </div>
          ))}
        </div>
      </div>

      {/* 停損建議 */}
      <div>
        <SectionTitle>停損建議（7-5停利五法）</SectionTitle>
        <div className="flex items-baseline gap-3 mb-2">
          <span className="text-lg font-semibold">{stop_loss.recommended}</span>
          <span className="font-mono text-lg">{stop_loss.price}</span>
          <span className="text-err text-sm">{distLabel(stop_loss.distance_pct)}</span>
        </div>
        <div className="text-xs text-muted mb-2">{stop_loss.reason}</div>
        <table className="w-full text-xs">
          <tbody>
            {stop_loss.alternatives.map((a: any, i: number) => (
              <tr key={i} className="border-t border-line/50">
                <td className="py-1 text-muted">{a.method}</td>
                <td className="py-1 font-mono text-right">{a.price}</td>
                <td className="py-1 text-right text-muted w-16">{distLabel(a.distance_pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* 總結 */}
      <div className="border-t border-line pt-3">
        <p className="text-sm leading-relaxed">{verdict}</p>
        <p className="text-xs text-muted mt-2">{disclaimer}</p>
      </div>
    </div>
  );
}
