import { useState, useEffect, useRef } from "react";
import { Bell, AlertTriangle, Info, AlertCircle, CheckCheck, TrendingUp, TrendingDown, BarChart3 } from "lucide-react";
import { C } from "../lib/format";
import { wsUrl, csrfHeaders } from "../api";

interface NotificationItem {
  id: number;
  level: "info" | "warning" | "error" | "trade" | "pnl";
  source: string;
  message: string;
  read: boolean;
  created_at: string;
}

const LEVEL_META: Record<string, { icon: typeof Info; color: string }> = {
  info: { icon: Info, color: C.blue },
  warning: { icon: AlertTriangle, color: "#d97706" },
  error: { icon: AlertCircle, color: C.red },
  trade: { icon: TrendingUp, color: C.green },
  pnl: { icon: BarChart3, color: C.blue },
};

// custom_strategy_scheduler.py sends level="trade"/"pnl" messages as exactly
// three lines: "Trade Opened/Closed/P&L Update — <strategy name>",
// "<symbol> (<mode>)", then a " | "-joined detail line (per-leg entries +
// qty, or exit reason + legs + pnl) — parsed back out here into a small
// table instead of one flat run-on line, since that's unreadable once a
// basket has 2+ legs.
function parseTradeMessage(message: string): { title: string; subtitle: string; rows: string[] } | null {
  const lines = message.split("\n");
  if (lines.length !== 3) return null;
  return { title: lines[0], subtitle: lines[1], rows: lines[2].split(" | ") };
}

const PNL_RE = /pnl=([+-]?\d+(?:\.\d+)?)%/;
function pnlSign(rows: string[]): number | null {
  for (const row of rows) {
    const m = PNL_RE.exec(row);
    if (m) return parseFloat(m[1]);
  }
  return null;
}

function timeAgo(iso: string): string {
  const diffMs = Date.now() - new Date(iso + "Z").getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function NotificationBell() {
  const [open, setOpen] = useState(false);
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const ref = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout>;

    // The backend (ws_notifications.py) pushes a message every 4s
    // unconditionally, even with nothing new, specifically so the client
    // can tell a live connection from a dead one. Browsers/proxies can
    // silently drop a WebSocket without ever firing onclose/onerror (a
    // "half-open" connection, e.g. after a laptop sleeps, a network
    // switch, or a NAT/proxy idling it out) — the socket looks OPEN but
    // will never receive another message, so new notifications stop
    // arriving until something else forces a fresh connection (which is
    // exactly the "count updates but the list doesn't, until I reload"
    // symptom this watchdog fixes). If nothing arrives for 3x the
    // server's own push interval, assume the connection is dead and
    // force a reconnect rather than waiting on a close event that may
    // never come.
    const STALE_AFTER_MS = 12_000;
    let lastMessageAt = Date.now();

    const connect = () => {
      if (cancelled) return;
      const ws = new WebSocket(wsUrl("/ws/notifications"));
      wsRef.current = ws;
      lastMessageAt = Date.now();
      ws.onmessage = (event) => {
        lastMessageAt = Date.now();
        try {
          const data = JSON.parse(event.data);
          if (data.type === "snapshot") {
            setNotifications(data.notifications || []);
            setUnreadCount(data.unread_count || 0);
          } else if (data.type === "update") {
            setUnreadCount(data.unread_count || 0);
            if (data.notifications?.length) {
              setNotifications((prev) => {
                const merged = [...data.notifications].reverse().concat(prev);
                return merged.slice(0, 50);
              });
            }
          }
        } catch (err) {
          console.error("Failed to parse /ws/notifications message", err);
        }
      };
      ws.onclose = () => {
        if (!cancelled) reconnectTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
    };
    connect();

    const watchdogTimer = setInterval(() => {
      if (cancelled) return;
      if (Date.now() - lastMessageAt > STALE_AFTER_MS) {
        wsRef.current?.close(); // triggers onclose -> the normal reconnect path above
      }
    }, 4000);

    return () => {
      cancelled = true;
      clearTimeout(reconnectTimer);
      clearInterval(watchdogTimer);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, []);

  const markRead = async (id: number) => {
    setNotifications((prev) => prev.map((n) => (n.id === id ? { ...n, read: true } : n)));
    setUnreadCount((c) => Math.max(0, c - 1));
    try {
      await fetch(`/api/notifications/${id}/read`, { method: "POST", credentials: "include", headers: csrfHeaders() });
    } catch {
      // WS delta on next poll will resync if this failed silently.
    }
  };

  const markAllRead = async () => {
    setNotifications((prev) => prev.map((n) => ({ ...n, read: true })));
    setUnreadCount(0);
    try {
      await fetch("/api/notifications/read-all", { method: "POST", credentials: "include", headers: csrfHeaders() });
    } catch {
      // WS delta on next poll will resync if this failed silently.
    }
  };

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="relative flex items-center focus:outline-none"
        title="Notifications"
      >
        <Bell size={17} style={{ color: unreadCount > 0 ? C.orange : C.text }} className="cursor-pointer hover:text-orange-500" />
        {unreadCount > 0 && (
          <span
            className="absolute -top-1.5 -right-1.5 min-w-[15px] h-[15px] px-1 rounded-full text-white text-[9px] font-bold flex items-center justify-center"
            style={{ backgroundColor: C.red }}
          >
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        )}
      </button>

      {open && (
        <div
          className="absolute right-0 z-40 w-96 bg-white rounded-xl shadow-lg border overflow-hidden"
          style={{ top: 28, borderColor: C.border2 }}
        >
          <div className="px-4 py-3 border-b flex items-center justify-between" style={{ borderColor: C.border }}>
            <span className="text-sm font-semibold text-gray-800">Notifications</span>
            {unreadCount > 0 && (
              <button onClick={markAllRead} className="flex items-center gap-1 text-[11px] font-medium hover:underline focus:outline-none" style={{ color: C.blue }}>
                <CheckCheck size={12} /> Mark all read
              </button>
            )}
          </div>
          <div style={{ maxHeight: 380, overflowY: "auto" }}>
            {notifications.length === 0 ? (
              <div className="px-4 py-10 text-center text-xs text-gray-400">No notifications yet.</div>
            ) : (
              notifications.map((n) => {
                const trade = (n.level === "trade" || n.level === "pnl") ? parseTradeMessage(n.message) : null;
                const closed = trade?.title.startsWith("Trade Closed");
                const opened = trade?.title.startsWith("Trade Opened");
                const sign = trade ? pnlSign(trade.rows) : null;
                // Opened: always the "started" green up-arrow — no P&L yet.
                // Closed/periodic update: color + direction follow the
                // actual sign so a profitable close/update reads as green
                // even though it's visually a "closed"/neutral event.
                const meta = trade
                  ? opened
                    ? { icon: TrendingUp, color: C.green }
                    : { icon: sign != null && sign < 0 ? TrendingDown : TrendingUp, color: sign == null ? C.blue : sign < 0 ? C.red : C.green }
                  : LEVEL_META[n.level] || LEVEL_META.info;
                const Icon = meta.icon;
                return (
                  <button
                    key={n.id}
                    onClick={() => !n.read && markRead(n.id)}
                    className="w-full flex items-start gap-3 px-4 py-3 text-left border-b last:border-0 transition-colors focus:outline-none"
                    style={{ borderColor: C.border, backgroundColor: n.read ? "transparent" : "#fff7ed" }}
                  >
                    <Icon size={15} style={{ color: meta.color }} className="shrink-0 mt-0.5" />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                          {trade ? (closed ? "Trade Closed" : opened ? "Trade Opened" : "P&L Update") : n.source.replace(/_/g, " ")}
                        </span>
                        <span className="text-[10px] text-gray-400 shrink-0">{timeAgo(n.created_at)}</span>
                      </div>
                      {trade ? (
                        <div className="mt-1">
                          <div className="text-xs font-semibold text-gray-800">{trade.title.replace(/^(Trade (Opened|Closed)|P&L Update) — /, "")}</div>
                          <div className="text-[11px] text-gray-500 mb-1">{trade.subtitle}</div>
                          <div className="rounded-md overflow-hidden border" style={{ borderColor: C.border2 }}>
                            {trade.rows.map((row, i) => {
                              const [key, ...rest] = row.split("=");
                              const isKeyValue = rest.length > 0;
                              return (
                                <div
                                  key={i}
                                  className="px-2 py-1 text-[11px] font-mono flex justify-between gap-2"
                                  style={{ backgroundColor: i % 2 === 0 ? C.hover : "transparent", color: C.text }}
                                >
                                  {isKeyValue ? (
                                    <>
                                      <span className="text-gray-500">{key}</span>
                                      <span className="font-semibold">{rest.join("=")}</span>
                                    </>
                                  ) : (
                                    <span>{row}</span>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      ) : (
                        <div className="text-xs text-gray-700 mt-0.5 leading-relaxed">{n.message}</div>
                      )}
                    </div>
                    {!n.read && <span className="w-2 h-2 rounded-full shrink-0 mt-1.5" style={{ backgroundColor: C.orange }} />}
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}
