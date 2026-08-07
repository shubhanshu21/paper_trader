import { useState, useEffect, useCallback } from "react";
import { Power, ShieldAlert } from "lucide-react";
import { api } from "../api";
import { C } from "../lib/format";

const POLL_MS = 10_000;

function timeAgo(iso: string): string {
  const diffMs = Date.now() - new Date(iso + "Z").getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ago`;
}

export default function KillSwitchControl() {
  const [active, setActive] = useState(false);
  const [reason, setReason] = useState<string | null>(null);
  const [activatedAt, setActivatedAt] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const status = await api.getKillSwitchStatus();
      setActive(status.active);
      setReason(status.reason);
      setActivatedAt(status.activated_at);
    } catch (err) {
      console.error("Failed to load kill switch status:", err);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const handleActivate = async () => {
    if (!confirm("Halt ALL new order flow across EVERY strategy (paper and live)? Already-open positions can still be closed manually. This does not auto-reset — you'll need to come back here to resume trading.")) {
      return;
    }
    setBusy(true);
    try {
      const status = await api.activateKillSwitch("Manual trigger from control panel");
      setActive(status.active);
      setReason(status.reason);
      setActivatedAt(status.activated_at);
    } catch (err) {
      console.error("Failed to activate kill switch:", err);
    } finally {
      setBusy(false);
    }
  };

  const handleReset = async () => {
    if (!confirm("Resume order flow for every strategy? Only do this once you've confirmed it's safe to trade again.")) {
      return;
    }
    setBusy(true);
    try {
      const status = await api.resetKillSwitch();
      setActive(status.active);
      setReason(status.reason);
      setActivatedAt(status.activated_at);
    } catch (err) {
      console.error("Failed to reset kill switch:", err);
    } finally {
      setBusy(false);
    }
  };

  if (active) {
    return (
      <button
        onClick={handleReset}
        disabled={busy}
        title={reason ? `${reason}${activatedAt ? ` — ${timeAgo(activatedAt)}` : ""}` : undefined}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold text-white animate-pulse disabled:opacity-60"
        style={{ backgroundColor: C.red }}
      >
        <ShieldAlert size={13} /> TRADING HALTED — Reset
      </button>
    );
  }

  return (
    <button
      onClick={handleActivate}
      disabled={busy}
      title="Halt all new order flow across every strategy immediately"
      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-semibold text-gray-500 border hover:border-red-300 hover:text-red-500 disabled:opacity-50"
      style={{ borderColor: C.border2 }}
    >
      <Power size={12} /> Kill Switch
    </button>
  );
}
