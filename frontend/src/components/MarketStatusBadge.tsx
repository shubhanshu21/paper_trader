import { useState, useEffect } from "react";
import { Circle } from "lucide-react";
import { api } from "../api";

const POLL_INTERVAL_MS = 30_000;

export default function MarketStatusBadge() {
  const [status, setStatus] = useState<{ open: boolean; message: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await api.marketStatus();
        if (!cancelled) setStatus(res);
      } catch {
        // Degrade silently — a failed status check just leaves the badge showing its last known state.
      }
    };
    load();
    const timer = setInterval(load, POLL_INTERVAL_MS);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);

  if (!status) return null;

  return (
    <div
      className="flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold select-none"
      style={{ backgroundColor: status.open ? "#e6f4ea" : "#fdecea", color: status.open ? "#1f8a3d" : "#c53030" }}
      title={status.message}
    >
      <Circle size={7} fill="currentColor" stroke="none" className={status.open ? "animate-pulse" : ""} />
      {status.open ? "Market Open" : "Market Closed"}
    </div>
  );
}
