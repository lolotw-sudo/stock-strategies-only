export function ActionBadge({ action }: { action: string }) {
  const map: Record<string, string> = {
    SELL: "badge-sell",
    BUY: "badge-buy",
    WATCH: "badge-watch",
    SKIP: "badge-skip",
    ERROR: "badge-err",
  };
  return <span className={map[action] || "badge-skip"}>{action}</span>;
}

export function SourceBadge({ source }: { source?: string }) {
  if (source === "ai") return <span className="badge-ai">AI</span>;
  if (source === "default") return <span className="badge-default">預設</span>;
  return <span className="badge-manual">手動</span>;
}
