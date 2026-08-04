import { useState, useEffect, useCallback } from "react";
import { Circle, RefreshCw } from "lucide-react";
import { api } from "../api";
import { useToast } from "../hooks/useToast";

// Coarser than MarketStatusBadge's 30s — this hits Upstox's own LTP
// endpoint for a real validity check (see routes_upstox_token.py's
// GET /status), not a free local computation, so it shouldn't be polled
// as aggressively as a plain server-clock read.
const POLL_INTERVAL_MS = 60_000;

export default function UpstoxTokenBadge() {
  const [status, setStatus] = useState<{ configured: boolean; valid: boolean } | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      const res = await api.upstoxTokenStatus();
      setStatus(res);
    } catch {
      // Degrade silently — a failed status check just leaves the badge showing its last known state.
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [load]);

  const handleClick = async () => {
    if (refreshing) return;
    if (status && !status.configured) {
      toast.error("Auto-login isn't configured (UPSTOX_USERNAME/PIN/TOTP_SECRET missing) — refresh the token manually via `python3 -m automate.auth.upstox_auth`.");
      return;
    }
    setRefreshing(true);
    try {
      const res = await api.refreshUpstoxToken();
      setStatus(res);
      if (res.timed_out) {
        toast.warning("Token refresh is taking longer than expected — it may still complete in the background. Check back shortly.");
      } else if (res.success) {
        toast.success("Upstox token refreshed successfully.");
      } else {
        toast.error("Token refresh failed — check the server logs (logs/api.log) for details.");
      }
    } catch {
      toast.error("Could not reach the server to refresh the token.");
    } finally {
      setRefreshing(false);
    }
  };

  if (!status) return null;

  const valid = status.valid;
  const label = refreshing ? "Refreshing…" : valid ? "Token Valid" : "Token Expired";
  const title = refreshing
    ? "Refreshing the Upstox access token…"
    : valid
    ? "Upstox access token is valid. Click to force a refresh anyway."
    : status.configured
    ? "Upstox access token has expired or is invalid — click to log in again."
    : "Upstox access token has expired or is invalid — auto-login isn't configured, click for instructions.";

  return (
    <button
      onClick={handleClick}
      disabled={refreshing}
      className="flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold select-none transition-opacity hover:opacity-80 focus:outline-none disabled:cursor-wait"
      style={{ backgroundColor: valid ? "#e6f4ea" : "#fdecea", color: valid ? "#1f8a3d" : "#c53030" }}
      title={title}
    >
      {refreshing ? (
        <RefreshCw size={9} className="animate-spin" />
      ) : (
        <Circle size={7} fill="currentColor" stroke="none" className={valid ? "animate-pulse" : ""} />
      )}
      {label}
    </button>
  );
}
