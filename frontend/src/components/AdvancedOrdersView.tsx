import { useState, useRef, useEffect } from "react";
import { Plus, X, Search, Repeat, TrendingDown, Layers, RefreshCw } from "lucide-react";
import { OcoOrder, TrailingStopOrder, BracketOrder, OrderLeg, Instrument, api } from "../api";
import { C, FONT, useToast, Select, Banner } from "./Common";

type OrderKind = "OCO" | "TRAILING_STOP" | "BRACKET";

const KIND_META: Record<OrderKind, { label: string; icon: typeof Repeat; color: string }> = {
  OCO: { label: "OCO", icon: Repeat, color: C.blue },
  TRAILING_STOP: { label: "Trailing Stop", icon: TrendingDown, color: C.orange },
  BRACKET: { label: "Bracket", icon: Layers, color: C.green },
};

const STATUS_META: Record<string, { bg: string; fg: string }> = {
  ACTIVE: { bg: "#e8f7ec", fg: "#1f8a3d" },
  COMPLETED: { bg: "#ecfdf5", fg: "#10b981" },
  CANCELLED: { bg: "#f5f5f5", fg: "#777777" },
};

function StatusPill({ status }: { status: string }) {
  const meta = STATUS_META[status] || STATUS_META.CANCELLED;
  return (
    <span className="inline-block px-2 py-0.5 text-[10px] font-bold rounded uppercase tracking-wider" style={{ background: meta.bg, color: meta.fg }}>
      {status}
    </span>
  );
}

function LegStatusPill({ status }: { status?: string }) {
  if (!status) return null;
  const meta: Record<string, { bg: string; fg: string }> = {
    PENDING: { bg: "#fff9e6", fg: "#a16a00" },
    PLACED: { bg: "#e2f2ff", fg: "#4184f3" },
    COMPLETE: { bg: "#ecfdf5", fg: "#10b981" },
    CANCELLED: { bg: "#f5f5f5", fg: "#777777" },
    REJECTED: { bg: "#fff1f0", fg: "#df514c" },
  };
  const m = meta[status] || meta.PENDING;
  return <span className="inline-block px-1.5 py-0.5 text-[9px] font-bold rounded" style={{ background: m.bg, color: m.fg }}>{status}</span>;
}

// -- Instrument search, matching StrategyBuilderModal's search+dropdown pattern --
function InstrumentPicker({
  value, label, onSelect,
}: { value: string; label: string; onSelect: (instrument: Instrument) => void }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Instrument[]>([]);
  const [open, setOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  useEffect(() => {
    if (!query || query.length < 2) { setResults([]); return; }
    let cancelled = false;
    setSearching(true);
    const t = setTimeout(async () => {
      try {
        const r = await api.searchInstruments(query);
        if (!cancelled) setResults(r);
      } catch {
        if (!cancelled) setResults([]);
      } finally {
        if (!cancelled) setSearching(false);
      }
    }, 300);
    return () => { cancelled = true; clearTimeout(t); };
  }, [query]);

  return (
    <div className="relative" ref={ref}>
      <label className="block text-xs font-medium text-gray-500 mb-1.5">{label}</label>
      <div className="relative">
        <span className="absolute inset-y-0 left-0 pl-3 flex items-center text-gray-400 pointer-events-none">
          <Search size={13} />
        </span>
        <input
          type="text"
          value={open ? query : value}
          onFocus={() => { setQuery(""); setOpen(true); }}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search instrument (e.g. RELIANCE, NIFTY 26AUG...)"
          className="w-full pl-8 pr-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-500"
          style={{ borderColor: C.border2 }}
        />
      </div>
      {open && query.length >= 2 && (
        <div className="absolute z-50 w-full mt-1 bg-white border rounded-lg shadow-lg max-h-56 overflow-y-auto" style={{ borderColor: C.border2 }}>
          {searching ? (
            <div className="px-4 py-3 text-xs text-gray-400">Searching...</div>
          ) : results.length === 0 ? (
            <div className="px-4 py-3 text-xs text-gray-400">No instruments match "{query}".</div>
          ) : (
            results.map((r) => (
              <button
                key={r.instrument_key}
                type="button"
                onClick={() => { onSelect(r); setOpen(false); setQuery(""); }}
                className="w-full flex items-center justify-between gap-2 px-4 py-2 text-left text-xs hover:bg-gray-50 transition-colors"
              >
                <span className="font-semibold text-gray-700">{r.symbol}</span>
                {r.last_price != null && <span className="text-gray-400 font-mono">₹{r.last_price.toFixed(2)}</span>}
              </button>
            ))
          )}
        </div>
      )}
      {!open && value && <p className="text-[11px] text-gray-400 mt-1 truncate">{value}</p>}
    </div>
  );
}

const emptyLeg = (transaction_type: "BUY" | "SELL"): OrderLeg => ({
  instrument_token: "", transaction_type, quantity: 1, order_type: "LIMIT", price: 0, trigger_price: 0, product: "D",
});

function LegEditor({
  title, leg, symbol, onChange, onSymbolChange,
}: { title: string; leg: OrderLeg; symbol: string; onChange: (leg: OrderLeg) => void; onSymbolChange: (s: string) => void }) {
  return (
    <div className="rounded-xl border p-3.5 space-y-3" style={{ borderColor: C.border2, background: C.hover }}>
      <div className="text-xs font-semibold text-gray-600">{title} <LegStatusPill status={leg.status} /></div>
      <InstrumentPicker
        value={symbol}
        label="Instrument"
        onSelect={(inst) => { onChange({ ...leg, instrument_token: inst.instrument_key }); onSymbolChange(inst.symbol); }}
      />
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1.5">Side</label>
          <Select
            value={leg.transaction_type}
            onChange={(v) => onChange({ ...leg, transaction_type: v as "BUY" | "SELL" })}
            options={[{ value: "BUY", label: "Buy" }, { value: "SELL", label: "Sell" }]}
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1.5">Quantity</label>
          <input type="number" min={1} value={leg.quantity}
            onChange={(e) => onChange({ ...leg, quantity: Number(e.target.value) })}
            className="w-full px-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-500"
            style={{ borderColor: C.border2 }} />
        </div>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1.5">Order Type</label>
          <Select
            value={leg.order_type}
            onChange={(v) => onChange({ ...leg, order_type: v as OrderLeg["order_type"] })}
            options={[
              { value: "MARKET", label: "Market" },
              { value: "LIMIT", label: "Limit" },
              { value: "SL", label: "SL (stop-limit)" },
              { value: "SL-M", label: "SL-M (stop-market)" },
            ]}
          />
        </div>
        {(leg.order_type === "LIMIT" || leg.order_type === "SL") && (
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1.5">Price</label>
            <input type="number" step="0.05" value={leg.price ?? 0}
              onChange={(e) => onChange({ ...leg, price: Number(e.target.value) })}
              className="w-full px-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-500"
              style={{ borderColor: C.border2 }} />
          </div>
        )}
        {(leg.order_type === "SL" || leg.order_type === "SL-M") && (
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1.5">Trigger Price</label>
            <input type="number" step="0.05" value={leg.trigger_price ?? 0}
              onChange={(e) => onChange({ ...leg, trigger_price: Number(e.target.value) })}
              className="w-full px-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-500"
              style={{ borderColor: C.border2 }} />
          </div>
        )}
      </div>
    </div>
  );
}

interface CreateModalProps {
  onClose: () => void;
  creating: boolean;
  onCreateOco: (req: { mode: "paper" | "live"; primary_order: OrderLeg; secondary_order: OrderLeg; strategy_name?: string }) => Promise<unknown>;
  onCreateTrailingStop: (req: { mode: "paper" | "live"; instrument_token: string; symbol: string; side: "BUY" | "SELL"; quantity: number; trail_amount: number; trail_type: "points" | "percentage"; product?: string; strategy_name?: string }) => Promise<unknown>;
  onCreateBracket: (req: { mode: "paper" | "live"; entry_order: OrderLeg; take_profit: OrderLeg; stop_loss: OrderLeg; strategy_name?: string }) => Promise<unknown>;
}

function CreateAdvancedOrderModal({ onClose, creating, onCreateOco, onCreateTrailingStop, onCreateBracket }: CreateModalProps) {
  const toast = useToast();
  const [kind, setKind] = useState<OrderKind>("OCO");
  const [mode, setMode] = useState<"paper" | "live">("paper");
  const [strategyName, setStrategyName] = useState("");
  const [error, setError] = useState("");

  // OCO
  const [primary, setPrimary] = useState<OrderLeg>(emptyLeg("SELL"));
  const [primarySymbol, setPrimarySymbol] = useState("");
  const [secondary, setSecondary] = useState<OrderLeg>(emptyLeg("BUY"));
  const [secondarySymbol, setSecondarySymbol] = useState("");

  // Trailing Stop
  const [tsInstrument, setTsInstrument] = useState("");
  const [tsSymbol, setTsSymbol] = useState("");
  const [tsSide, setTsSide] = useState<"BUY" | "SELL">("BUY");
  const [tsQuantity, setTsQuantity] = useState(1);
  const [tsTrailAmount, setTsTrailAmount] = useState(5);
  const [tsTrailType, setTsTrailType] = useState<"points" | "percentage">("points");

  // Bracket
  const [entry, setEntry] = useState<OrderLeg>({ ...emptyLeg("SELL"), order_type: "MARKET" });
  const [entrySymbol, setEntrySymbol] = useState("");
  const [takeProfit, setTakeProfit] = useState<OrderLeg>({ ...emptyLeg("BUY"), order_type: "LIMIT" });
  const [tpSymbol, setTpSymbol] = useState("");
  const [stopLoss, setStopLoss] = useState<OrderLeg>({ ...emptyLeg("BUY"), order_type: "SL-M" });
  const [slSymbol, setSlSymbol] = useState("");

  const submit = async () => {
    setError("");
    try {
      if (kind === "OCO") {
        if (!primary.instrument_token || !secondary.instrument_token) throw new Error("Select an instrument for both legs.");
        await onCreateOco({ mode, primary_order: primary, secondary_order: secondary, strategy_name: strategyName || undefined });
      } else if (kind === "TRAILING_STOP") {
        if (!tsInstrument) throw new Error("Select an instrument.");
        await onCreateTrailingStop({
          mode, instrument_token: tsInstrument, symbol: tsSymbol, side: tsSide,
          quantity: tsQuantity, trail_amount: tsTrailAmount, trail_type: tsTrailType,
          strategy_name: strategyName || undefined,
        });
      } else {
        if (!entry.instrument_token || !takeProfit.instrument_token || !stopLoss.instrument_token) throw new Error("Select an instrument for entry, take-profit, and stop-loss.");
        await onCreateBracket({ mode, entry_order: entry, take_profit: takeProfit, stop_loss: stopLoss, strategy_name: strategyName || undefined });
      }
      toast.success(`${KIND_META[kind].label} order created successfully`);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create order.");
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-40 backdrop-blur-sm p-4">
      <div className="bg-white rounded-2xl w-full max-w-2xl max-h-[90vh] flex flex-col border shadow-2xl" style={{ borderColor: C.border, ...FONT }}>
        <div className="px-6 py-4 border-b flex items-center justify-between shrink-0" style={{ borderColor: C.border2 }}>
          <h3 className="text-base font-bold text-gray-800">New Advanced Order</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 focus:outline-none"><X size={20} /></button>
        </div>

        <div className="overflow-y-auto flex-1 p-6 space-y-4">
          <div className="grid grid-cols-3 gap-2">
            {(Object.keys(KIND_META) as OrderKind[]).map((k) => {
              const meta = KIND_META[k];
              const Icon = meta.icon;
              const active = kind === k;
              return (
                <button key={k} type="button" onClick={() => setKind(k)}
                  className="flex flex-col items-center gap-1.5 px-3 py-3 rounded-xl border text-xs font-semibold transition-colors focus:outline-none"
                  style={{ borderColor: active ? meta.color : C.border2, background: active ? `${meta.color}0d` : "#fff", color: active ? meta.color : C.text }}>
                  <Icon size={16} />
                  {meta.label}
                </button>
              );
            })}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1.5">Mode</label>
              <Select value={mode} onChange={(v) => setMode(v as "paper" | "live")} options={[
                { value: "paper", label: "Paper Trading" },
                { value: "live", label: "Live (real broker orders)" },
              ]} />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1.5">Strategy Name (optional)</label>
              <input value={strategyName} onChange={(e) => setStrategyName(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-500"
                style={{ borderColor: C.border2 }} placeholder="e.g. Manual hedge" />
            </div>
          </div>
          {mode === "live" && (
            <div className="flex items-center gap-2 px-3 py-2 rounded-lg text-xs" style={{ background: "#fff9e6", color: "#a16a00" }}>
              Live mode places real orders at the broker immediately.
            </div>
          )}

          {kind === "OCO" && (
            <div className="space-y-3">
              <p className="text-xs text-gray-500">Two orders where a fill on one automatically cancels the other — typically a take-profit and a stop-loss on the same position.</p>
              <LegEditor title="Primary Order" leg={primary} symbol={primarySymbol} onChange={setPrimary} onSymbolChange={setPrimarySymbol} />
              <LegEditor title="Secondary Order" leg={secondary} symbol={secondarySymbol} onChange={setSecondary} onSymbolChange={setSecondarySymbol} />
            </div>
          )}

          {kind === "TRAILING_STOP" && (
            <div className="space-y-3">
              <p className="text-xs text-gray-500">The exit stop follows the market price by a fixed trail as it moves in your favor.</p>
              <InstrumentPicker value={tsSymbol} label="Instrument" onSelect={(inst) => { setTsInstrument(inst.instrument_key); setTsSymbol(inst.symbol); }} />
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-gray-500 mb-1.5">Position Side Being Protected</label>
                  <Select value={tsSide} onChange={(v) => setTsSide(v as "BUY" | "SELL")} options={[
                    { value: "BUY", label: "Long (exits via Sell)" },
                    { value: "SELL", label: "Short (exits via Buy)" },
                  ]} />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-500 mb-1.5">Quantity</label>
                  <input type="number" min={1} value={tsQuantity} onChange={(e) => setTsQuantity(Number(e.target.value))}
                    className="w-full px-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-500" style={{ borderColor: C.border2 }} />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-gray-500 mb-1.5">Trail Amount</label>
                  <input type="number" step="0.5" min={0.01} value={tsTrailAmount} onChange={(e) => setTsTrailAmount(Number(e.target.value))}
                    className="w-full px-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-500" style={{ borderColor: C.border2 }} />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-500 mb-1.5">Trail Type</label>
                  <Select value={tsTrailType} onChange={(v) => setTsTrailType(v as "points" | "percentage")} options={[
                    { value: "points", label: "Points" },
                    { value: "percentage", label: "Percentage" },
                  ]} />
                </div>
              </div>
            </div>
          )}

          {kind === "BRACKET" && (
            <div className="space-y-3">
              <p className="text-xs text-gray-500">An entry order that, once filled, automatically arms a take-profit / stop-loss OCO pair.</p>
              <LegEditor title="Entry Order" leg={entry} symbol={entrySymbol} onChange={setEntry} onSymbolChange={setEntrySymbol} />
              <LegEditor title="Take Profit" leg={takeProfit} symbol={tpSymbol} onChange={setTakeProfit} onSymbolChange={setTpSymbol} />
              <LegEditor title="Stop Loss" leg={stopLoss} symbol={slSymbol} onChange={setStopLoss} onSymbolChange={setSlSymbol} />
            </div>
          )}

          {error && <p className="text-xs font-medium" style={{ color: C.red }}>{error}</p>}
        </div>

        <div className="px-6 py-4 border-t flex justify-end gap-2 shrink-0" style={{ borderColor: C.border2 }}>
          <button onClick={onClose} className="px-3.5 py-1.5 text-xs font-semibold rounded-lg bg-gray-100 text-gray-600 hover:bg-gray-200 transition-colors focus:outline-none">
            Cancel
          </button>
          <button onClick={submit} disabled={creating}
            className="flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-semibold rounded-lg text-white transition-colors focus:outline-none hover:opacity-90 disabled:opacity-50"
            style={{ backgroundColor: C.orange }}>
            {creating ? "Creating..." : "Create Order"}
          </button>
        </div>
      </div>
    </div>
  );
}

interface AdvancedOrdersProps {
  ocoOrders: OcoOrder[];
  trailingStops: TrailingStopOrder[];
  bracketOrders: BracketOrder[];
  loading: boolean;
  creating: boolean;
  cancellingId: string | null;
  onRefresh: () => void;
  onCreateOco: CreateModalProps["onCreateOco"];
  onCreateTrailingStop: CreateModalProps["onCreateTrailingStop"];
  onCreateBracket: CreateModalProps["onCreateBracket"];
  onCancelOco: (id: string) => void;
  onCancelTrailingStop: (id: string) => void;
  onCancelBracket: (id: string) => void;
}

type UnifiedRow = {
  id: string;
  kind: OrderKind;
  mode: string;
  status: string;
  strategy_name: string | null;
  created_at: string | null;
  summary: React.ReactNode;
};

export default function AdvancedOrdersView(props: AdvancedOrdersProps) {
  const {
    ocoOrders, trailingStops, bracketOrders, loading, creating, cancellingId, onRefresh,
    onCreateOco, onCreateTrailingStop, onCreateBracket, onCancelOco, onCancelTrailingStop, onCancelBracket,
  } = props;
  const [showCreate, setShowCreate] = useState(false);

  useEffect(() => { onRefresh(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const rows: UnifiedRow[] = [
    ...ocoOrders.map((o): UnifiedRow => ({
      id: o.id, kind: "OCO", mode: o.mode, status: o.status, strategy_name: o.strategy_name, created_at: o.created_at,
      summary: (
        <span className="flex items-center gap-1.5 flex-wrap">
          <span className="font-mono text-[11px]">{o.primary_order.transaction_type} {o.primary_order.instrument_token}</span>
          <LegStatusPill status={o.primary_order.status} />
          <span className="text-gray-300">/</span>
          <span className="font-mono text-[11px]">{o.secondary_order.transaction_type} {o.secondary_order.instrument_token}</span>
          <LegStatusPill status={o.secondary_order.status} />
        </span>
      ),
    })),
    ...trailingStops.map((t): UnifiedRow => ({
      id: t.id, kind: "TRAILING_STOP", mode: t.mode, status: t.status, strategy_name: t.strategy_name, created_at: t.created_at,
      summary: (
        <span className="text-[11px]">
          {t.symbol} · {t.side} · trail {t.trail_amount}{t.trail_type === "percentage" ? "%" : "pts"}
          {t.current_stop_price != null && <span className="text-gray-400"> · stop ₹{t.current_stop_price.toFixed(2)}</span>}
        </span>
      ),
    })),
    ...bracketOrders.map((b): UnifiedRow => ({
      id: b.id, kind: "BRACKET", mode: b.mode, status: b.status, strategy_name: b.strategy_name, created_at: b.created_at,
      summary: (
        <span className="flex items-center gap-1.5 flex-wrap">
          <span className="font-mono text-[11px]">{b.entry_order.transaction_type} {b.entry_order.instrument_token}</span>
          <LegStatusPill status={b.entry_order.status} />
          <span className="text-gray-300">→ TP</span>
          <LegStatusPill status={b.take_profit.status} />
          <span className="text-gray-300">/ SL</span>
          <LegStatusPill status={b.stop_loss.status} />
        </span>
      ),
    })),
  ].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));

  const handleCancel = (row: UnifiedRow) => {
    if (row.kind === "OCO") onCancelOco(row.id);
    else if (row.kind === "TRAILING_STOP") onCancelTrailingStop(row.id);
    else onCancelBracket(row.id);
  };

  return (
    <div className="w-full" style={FONT}>
      <div className="flex items-start justify-between mb-5 flex-wrap gap-3">
        <div>
          <h1 className="text-3xl font-light text-gray-800">Advanced Orders</h1>
          <p className="text-xs text-gray-500 mt-1">OCO, trailing-stop, and bracket orders — driven live against the broker, or simulated tick-by-tick in paper mode.</p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={onRefresh} disabled={loading}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold transition-colors focus:outline-none hover:opacity-80"
            style={{ backgroundColor: C.hover, color: C.text }}>
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} /> Refresh
          </button>
          <button onClick={() => setShowCreate(true)}
            className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold text-white transition-all focus:outline-none hover:opacity-90 shadow-sm"
            style={{ backgroundColor: C.orange }}>
            <Plus size={16} /> New Order
          </button>
        </div>
      </div>

      <Banner />

      {rows.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-center bg-white border border-dashed rounded-lg p-10 select-none">
          <Layers size={32} className="mb-4" style={{ color: C.border2 }} />
          <h3 className="text-base font-normal text-gray-700 mb-2">No advanced orders yet</h3>
          <p className="text-xs text-gray-400 max-w-sm mb-6">Create an OCO, trailing-stop, or bracket order to automate exits and entries beyond a single market/limit order.</p>
          <button onClick={() => setShowCreate(true)}
            className="px-5 py-2.5 text-xs font-bold text-white rounded-lg transition-colors shadow-sm focus:outline-none hover:opacity-90"
            style={{ backgroundColor: C.orange }}>
            New Order
          </button>
        </div>
      ) : (
        <div className="overflow-x-auto bg-white rounded-xl border shadow-sm" style={{ borderColor: C.tableBorder }}>
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b text-xs" style={{ borderColor: C.border2, background: C.tableHeaderBg }}>
                <th className="px-4 py-3 text-left font-medium text-[12px]" style={{ color: C.tableHeaderText }}>Type</th>
                <th className="px-4 py-3 text-left font-medium text-[12px]" style={{ color: C.tableHeaderText }}>Details</th>
                <th className="px-4 py-3 text-left font-medium text-[12px]" style={{ color: C.tableHeaderText }}>Mode</th>
                <th className="px-4 py-3 text-left font-medium text-[12px]" style={{ color: C.tableHeaderText }}>Strategy</th>
                <th className="px-4 py-3 text-left font-medium text-[12px]" style={{ color: C.tableHeaderText }}>Status</th>
                <th className="px-4 py-3 text-right font-medium text-[12px]" style={{ color: C.tableHeaderText }}>Action</th>
              </tr>
            </thead>
            <tbody className="divide-y" style={{ borderColor: C.border }}>
              {rows.map((row) => {
                const meta = KIND_META[row.kind];
                const Icon = meta.icon;
                const isCancelling = cancellingId === row.id;
                return (
                  <tr key={row.id} className="hover:bg-gray-50 transition-colors">
                    <td className="px-4 py-3 text-[13px]" style={{ borderBottom: `1px solid ${C.border}` }}>
                      <span className="flex items-center gap-1.5 font-semibold" style={{ color: meta.color }}>
                        <Icon size={13} /> {meta.label}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-[13px]" style={{ borderBottom: `1px solid ${C.border}` }}>{row.summary}</td>
                    <td className="px-4 py-3 text-[13px] uppercase text-gray-500" style={{ borderBottom: `1px solid ${C.border}` }}>{row.mode}</td>
                    <td className="px-4 py-3 text-[13px] text-gray-500" style={{ borderBottom: `1px solid ${C.border}` }}>{row.strategy_name || "—"}</td>
                    <td className="px-4 py-3 text-[13px]" style={{ borderBottom: `1px solid ${C.border}` }}><StatusPill status={row.status} /></td>
                    <td className="px-4 py-3 text-right text-[13px]" style={{ borderBottom: `1px solid ${C.border}` }}>
                      {row.status === "ACTIVE" && (
                        <button onClick={() => handleCancel(row)} disabled={isCancelling}
                          className="px-2.5 py-1 text-[11px] font-bold text-white rounded bg-red-500 hover:bg-red-600 disabled:bg-gray-300 transition-colors shadow-sm focus:outline-none"
                          style={{ backgroundColor: isCancelling ? undefined : C.red }}>
                          {isCancelling ? "..." : "Cancel"}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {showCreate && (
        <CreateAdvancedOrderModal
          onClose={() => setShowCreate(false)}
          creating={creating}
          onCreateOco={onCreateOco}
          onCreateTrailingStop={onCreateTrailingStop}
          onCreateBracket={onCreateBracket}
        />
      )}
    </div>
  );
}
