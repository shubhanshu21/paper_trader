import { useState, useEffect, useRef, useCallback } from "react";
import { AlarmClock, Plus, Trash2, CheckCircle2 } from "lucide-react";
import { api, ApiError, PriceAlertRow, PriceAlertCondition } from "../api";
import { C } from "../lib/format";
import SymbolSearchInput from "./SymbolSearchInput";

const CONDITION_LABELS: Record<PriceAlertCondition, string> = {
  ABOVE: "goes above",
  BELOW: "goes below",
  CROSSES_ABOVE: "crosses above",
  CROSSES_BELOW: "crosses below",
};

function timeAgo(iso: string): string {
  const diffMs = Date.now() - new Date(iso + "Z").getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function PriceAlertsBell() {
  const [open, setOpen] = useState(false);
  const [alerts, setAlerts] = useState<PriceAlertRow[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [symbol, setSymbol] = useState("NIFTY");
  const [condition, setCondition] = useState<PriceAlertCondition>("ABOVE");
  const [targetPrice, setTargetPrice] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const activeCount = alerts.filter((a) => a.status === "ACTIVE").length;

  const load = useCallback(async () => {
    try {
      const res = await api.getPriceAlerts();
      setAlerts(res.alerts);
    } catch (err) {
      console.error("Failed to load price alerts:", err);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  // Re-poll whenever the panel opens — a background-triggered alert
  // (Telegram/notification bell) won't otherwise update this list until
  // the next full page load.
  useEffect(() => { if (open) load(); }, [open, load]);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) { setOpen(false); setShowForm(false); }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const createAlert = async () => {
    const price = parseFloat(targetPrice);
    if (!symbol || !price || price <= 0) {
      setError("Enter a symbol and a valid target price.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const created = await api.createPriceAlert({ symbol, condition, target_price: price, note: note || undefined });
      setAlerts((prev) => [created, ...prev]);
      setShowForm(false);
      setTargetPrice("");
      setNote("");
    } catch (err) {
      const detail = err instanceof ApiError && err.detail ? (Array.isArray(err.detail) ? err.detail.join(" ") : err.detail) : null;
      setError(detail || (err instanceof Error ? err.message : "Could not create alert."));
    } finally {
      setSaving(false);
    }
  };

  const removeAlert = async (id: number) => {
    setAlerts((prev) => prev.filter((a) => a.id !== id));
    try {
      await api.deletePriceAlert(id);
    } catch {
      load(); // resync if the delete actually failed server-side
    }
  };

  return (
    <div className="relative" ref={ref}>
      <button type="button" onClick={() => setOpen((o) => !o)} className="relative flex items-center focus:outline-none" title="Price Alerts" aria-label={`Price alerts${activeCount > 0 ? ` (${activeCount} active)` : ""}`}>
        <AlarmClock size={17} style={{ color: activeCount > 0 ? C.orange : C.text }} className="cursor-pointer hover:text-orange-500" />
        {activeCount > 0 && (
          <span className="absolute -top-1.5 -right-1.5 min-w-[15px] h-[15px] px-1 rounded-full text-white text-[9px] font-bold flex items-center justify-center" style={{ backgroundColor: C.blue }}>
            {activeCount > 99 ? "99+" : activeCount}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 z-40 w-96 bg-white rounded-xl shadow-lg border overflow-hidden" style={{ top: 28, borderColor: C.border2 }}>
          <div className="px-4 py-3 border-b flex items-center justify-between" style={{ borderColor: C.border }}>
            <span className="text-sm font-semibold text-gray-800">Price Alerts</span>
            <button onClick={() => setShowForm((s) => !s)} className="flex items-center gap-1 text-[11px] font-medium hover:underline focus:outline-none" style={{ color: C.blue }}>
              <Plus size={12} /> New Alert
            </button>
          </div>

          {showForm && (
            <div className="px-4 py-3 border-b space-y-2" style={{ borderColor: C.border, backgroundColor: "#fafafa" }}>
              {error && <p className="text-[11px] font-semibold" style={{ color: C.red }}>{error}</p>}
              <SymbolSearchInput value={symbol} onChange={setSymbol} className="w-full" scope="ALL" />
              <div className="flex gap-2">
                <select
                  value={condition}
                  onChange={(e) => setCondition(e.target.value as PriceAlertCondition)}
                  className="flex-1 text-xs font-semibold border rounded-md px-2 py-1.5"
                  style={{ borderColor: C.border2 }}
                >
                  {(Object.keys(CONDITION_LABELS) as PriceAlertCondition[]).map((c) => (
                    <option key={c} value={c}>{CONDITION_LABELS[c]}</option>
                  ))}
                </select>
                <input
                  type="number" placeholder="Target price" value={targetPrice}
                  onChange={(e) => setTargetPrice(e.target.value)}
                  className="w-28 text-xs font-semibold border rounded-md px-2 py-1.5"
                  style={{ borderColor: C.border2 }}
                />
              </div>
              <input
                type="text" placeholder="Note (optional)" value={note}
                onChange={(e) => setNote(e.target.value)}
                className="w-full text-xs border rounded-md px-2 py-1.5"
                style={{ borderColor: C.border2 }}
              />
              <button
                onClick={createAlert} disabled={saving}
                className="w-full py-1.5 rounded-md text-xs font-semibold text-white disabled:opacity-50"
                style={{ backgroundColor: C.orange }}
              >
                {saving ? "Creating..." : "Create Alert"}
              </button>
            </div>
          )}

          <div style={{ maxHeight: 340, overflowY: "auto" }}>
            {alerts.length === 0 ? (
              <div className="px-4 py-10 text-center text-xs text-gray-400">No alerts yet — click "New Alert" to get notified when a symbol hits a price.</div>
            ) : (
              alerts.map((a) => (
                <div key={a.id} className="flex items-start gap-3 px-4 py-3 border-b last:border-0" style={{ borderColor: C.border }}>
                  {a.status === "TRIGGERED" ? (
                    <CheckCircle2 size={15} style={{ color: C.green }} className="shrink-0 mt-0.5" />
                  ) : (
                    <AlarmClock size={15} style={{ color: C.blue }} className="shrink-0 mt-0.5" />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="text-xs font-semibold text-gray-800">
                      {a.symbol} {CONDITION_LABELS[a.condition]} {a.target_price}
                    </div>
                    {a.note && <div className="text-[11px] text-gray-500">{a.note}</div>}
                    <div className="text-[10px] text-gray-400 mt-0.5">
                      {a.status === "TRIGGERED" && a.triggered_at
                        ? `Triggered at ${a.triggered_price} · ${timeAgo(a.triggered_at)}`
                        : `Watching · created ${timeAgo(a.created_at)}`}
                    </div>
                  </div>
                  <button onClick={() => removeAlert(a.id)} className="p-1 rounded hover:bg-red-50 text-red-400 shrink-0" aria-label={`Delete alert for ${a.symbol}`}><Trash2 size={13} /></button>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
