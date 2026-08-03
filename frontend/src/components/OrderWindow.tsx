import { useState } from "react";
import { X, RotateCcw, Layers, Tag, ChevronDown } from "lucide-react";
import { C, FONT, inr } from "../lib/format";

interface OrderWindowProps {
  order: { symbol: string; exch: string; side: "BUY" | "SELL"; instrument_key: string; last_price: number };
  onClose: () => void;
  onPlaceOrder: (qty: number, price: number, product: string, mode: string, side: "BUY" | "SELL") => Promise<void>;
}

type MainTab = "Quick" | "Regular";

export default function OrderWindow({ order, onClose, onPlaceOrder }: OrderWindowProps) {
  const [side, setSide] = useState<"BUY" | "SELL">(order.side);
  const [tab, setTab] = useState<MainTab>("Regular");
  const [qty, setQty] = useState("1");
  const [price, setPrice] = useState(order.last_price > 0 ? order.last_price.toFixed(2) : "0.05");
  const [triggerPrice, setTriggerPrice] = useState("0");
  const [product, setProduct] = useState<"MIS" | "CNC">("CNC"); // Intraday = MIS, Longterm = CNC
  const [orderType, setOrderType] = useState<"MARKET" | "LIMIT" | "SL" | "SL-M">("LIMIT");
  const [mode, setMode] = useState("paper"); // 'paper' | 'live'
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const buy = side === "BUY";
  const accent = buy ? C.blue : C.orange;
  const isSl = orderType === "SL" || orderType === "SL-M";
  const amount = (parseFloat(price) || 0) * (parseInt(qty) || 0);

  const handleSubmit = async () => {
    setError("");
    const quantity = parseInt(qty);
    const parsedPrice = parseFloat(price);

    if (isNaN(quantity) || quantity <= 0) {
      setError("Please specify a valid positive quantity.");
      return;
    }
    if (orderType === "LIMIT" && (isNaN(parsedPrice) || parsedPrice <= 0)) {
      setError("Please specify a valid positive execution price.");
      return;
    }

    setLoading(true);
    try {
      // Backend always executes at LTP (market); price/trigger/SL fields are
      // captured for display parity but not sent as limit/trigger instructions.
      await onPlaceOrder(quantity, orderType === "MARKET" ? order.last_price : parsedPrice, product, mode, side);
    } catch (err) {
      const message = err instanceof Error ? err.message : undefined;
      setError(message || "Failed to submit trade.");
    } finally {
      setLoading(false);
    }
  };

  const mainTabs: MainTab[] = ["Quick", "Regular"];

  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center pt-24 bg-black/40 backdrop-blur-xs" style={FONT}>
      <div
        className={tab === "Quick" ? "w-80 bg-white shadow-2xl rounded overflow-hidden" : "w-[600px] bg-white shadow-2xl rounded overflow-hidden"}
      >
        {/* header */}
        <div className="flex items-center justify-between px-6 py-4" style={{ background: accent }}>
          <div>
            <div className="text-lg font-semibold text-white tracking-wide">{order.symbol}</div>
            <div className="text-xs text-white/80 font-medium">
              {order.exch} <span className="ml-2 font-mono">₹{inr(order.last_price)}</span>
            </div>
          </div>
          <div className="flex items-center gap-4">
            {/* Side toggle switch */}
            <button
              onClick={() => setSide(buy ? "SELL" : "BUY")}
              className="relative rounded-full border border-white/20 p-0.5 focus:outline-none"
              style={{ width: 42, height: 22, background: "rgba(255,255,255,.2)" }}
              title="Toggle Buy / Sell"
            >
              <span
                className="absolute bg-white rounded-full transition-all duration-200"
                style={{ width: 16, height: 16, top: 2.5, left: buy ? 3 : 21 }}
              />
            </button>
            <button onClick={onClose} className="text-white/80 hover:text-white focus:outline-none">
              <X size={20} />
            </button>
          </div>
        </div>

        {/* tabs */}
        <div
          className="flex items-center justify-between px-6 border-b"
          style={{ background: "#fafafa", borderColor: C.border2 }}
        >
          <div className="flex gap-6">
            {mainTabs.map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className="py-3 text-sm font-semibold focus:outline-none"
                style={{
                  color: tab === t ? accent : C.muted,
                  borderBottom: tab === t ? `2.5px solid ${accent}` : "2.5px solid transparent",
                }}
              >
                {t}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-1 text-xs font-medium" style={{ color: C.muted }}>
            <Tag size={12} />
            Tags
          </div>
        </div>

        <div className="p-6 space-y-4">
          {error && (
            <div className="bg-red-50 border border-red-200 text-red-600 px-4 py-3 rounded text-xs font-semibold">
              {error}
            </div>
          )}

          {tab === "Quick" ? (
            <div className="space-y-4">
              <div>
                <label className="block text-xs font-semibold text-gray-500 mb-1">Quantity</label>
                <input
                  type="number"
                  className="w-full px-3 py-2 border rounded focus:ring-1 focus:ring-orange-500 outline-none text-sm font-semibold text-gray-800 font-mono"
                  value={qty}
                  onChange={(e) => setQty(e.target.value)}
                />
              </div>
              <div>
                <label className="block text-xs font-semibold text-gray-500 mb-1">Execution Price</label>
                <input
                  type="number"
                  step="0.05"
                  className="w-full px-3 py-2 border rounded focus:ring-1 focus:ring-orange-500 outline-none text-sm font-semibold text-gray-800 font-mono"
                  value={price}
                  onChange={(e) => setPrice(e.target.value)}
                />
              </div>
              <button
                onClick={handleSubmit}
                disabled={loading}
                className="w-full py-2.5 text-sm font-semibold text-white rounded shadow transition-all flex items-center justify-center gap-2"
                style={{ background: accent }}
              >
                {loading ? <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" /> : null}
                {buy ? "Quick BUY Order" : "Quick SELL Order"}
              </button>
            </div>
          ) : (
            <>
              {/* Product selector (Intraday / Longterm) + Advanced */}
              <div className="flex items-center justify-between pb-1">
                <div className="flex items-center gap-6">
                  <label className="flex items-center gap-2 text-sm text-gray-700 font-medium cursor-pointer">
                    <input
                      type="radio"
                      name="product"
                      checked={product === "MIS"}
                      onChange={() => setProduct("MIS")}
                      style={{ accentColor: accent }}
                    />
                    Intraday <span className="text-gray-400 font-normal">MIS</span>
                  </label>
                  <label className="flex items-center gap-2 text-sm text-gray-700 font-medium cursor-pointer">
                    <input
                      type="radio"
                      name="product"
                      checked={product === "CNC"}
                      onChange={() => setProduct("CNC")}
                      style={{ accentColor: accent }}
                    />
                    Longterm <span className="text-gray-400 font-normal">CNC</span>
                  </label>
                </div>
                <div className="flex items-center gap-1 text-xs font-semibold cursor-default select-none" style={{ color: accent }}>
                  Advanced
                  <ChevronDown size={13} />
                </div>
              </div>

              {/* Qty / Price / Trigger price */}
              <div className="grid grid-cols-3 gap-4">
                <div>
                  <label className="block text-xs font-semibold text-gray-500 mb-1">Qty.</label>
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      className="w-full px-3 py-2 border rounded focus:ring-1 outline-none text-sm font-semibold text-gray-800 font-mono"
                      style={{ borderColor: C.border2 }}
                      value={qty}
                      onChange={(e) => setQty(e.target.value)}
                    />
                    <button
                      type="button"
                      title="Not supported yet"
                      disabled
                      className="p-2 border rounded text-gray-400 cursor-not-allowed"
                      style={{ borderColor: C.border2 }}
                    >
                      <Layers size={15} />
                    </button>
                  </div>
                </div>
                <div>
                  <label className="block text-xs font-semibold text-gray-500 mb-1">Price</label>
                  <input
                    type="number"
                    step="0.05"
                    disabled={orderType === "MARKET"}
                    className="w-full px-3 py-2 border rounded focus:ring-1 outline-none text-sm font-semibold text-gray-800 font-mono disabled:bg-gray-50 disabled:text-gray-400"
                    style={{ borderColor: C.border2 }}
                    value={orderType === "MARKET" ? "" : price}
                    placeholder={orderType === "MARKET" ? "Market" : undefined}
                    onChange={(e) => setPrice(e.target.value)}
                  />
                </div>
                <div>
                  <label className="block text-xs font-semibold text-gray-500 mb-1">Trigger price</label>
                  <input
                    type="number"
                    step="0.05"
                    disabled={!isSl}
                    className="w-full px-3 py-2 border rounded focus:ring-1 outline-none text-sm font-semibold text-gray-800 font-mono disabled:bg-gray-50 disabled:text-gray-300"
                    style={{ borderColor: C.border2 }}
                    value={isSl ? triggerPrice : "0"}
                    onChange={(e) => setTriggerPrice(e.target.value)}
                  />
                </div>
              </div>

              {/* Order type: Market / Limit ... SL / SL-M */}
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-6">
                  {(["MARKET", "LIMIT"] as const).map((ot) => (
                    <label key={ot} className="flex items-center gap-2 text-sm text-gray-700 font-medium cursor-pointer">
                      <input
                        type="radio"
                        name="orderType"
                        checked={orderType === ot}
                        onChange={() => setOrderType(ot)}
                        style={{ accentColor: accent }}
                      />
                      {ot === "MARKET" ? "Market" : "Limit"}
                    </label>
                  ))}
                </div>
                <div className="flex items-center gap-6">
                  {(["SL", "SL-M"] as const).map((ot) => (
                    <label key={ot} className="flex items-center gap-2 text-sm text-gray-700 font-medium cursor-pointer">
                      <input
                        type="radio"
                        name="orderType"
                        checked={orderType === ot}
                        onChange={() => setOrderType(ot)}
                        style={{ accentColor: accent }}
                      />
                      {ot}
                    </label>
                  ))}
                </div>
              </div>

              {/* Mode selector */}
              <div className="flex items-center gap-6 border-t pt-4" style={{ borderColor: C.border2 }}>
                <span className="text-xs font-semibold text-gray-400 uppercase tracking-wider">Broker Mode:</span>
                {[
                  ["paper", "Virtual Paper Trading"],
                  ["live", "Live Broker (Upstox EQ)"],
                ].map(([val, label]) => (
                  <label key={val} className="flex items-center gap-2 text-sm text-gray-700 font-medium cursor-pointer">
                    <input
                      type="radio"
                      name="mode"
                      checked={mode === val}
                      onChange={() => setMode(val)}
                      style={{ accentColor: accent }}
                    />
                    {label}
                  </label>
                ))}
              </div>

              {/* Footer */}
              <div className="flex items-center justify-between border-t pt-5 mt-1" style={{ borderColor: C.border2 }}>
                <div className="text-xs text-gray-400 flex items-center gap-3 font-medium">
                  <span>
                    Amount <span className="text-gray-700 font-semibold">{orderType === "MARKET" ? "N/A" : `₹${inr(amount)}`}</span>
                  </span>
                  <span>
                    Charges <span className="text-gray-700 font-semibold">N/A</span>
                  </span>
                  <RotateCcw size={12} className="cursor-default text-gray-400" />
                </div>
                <div className="flex gap-3">
                  <button
                    onClick={onClose}
                    className="px-5 py-2 text-sm font-semibold border rounded hover:bg-gray-50 transition-colors focus:outline-none"
                    style={{ borderColor: C.border2 }}
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleSubmit}
                    disabled={loading}
                    className="px-8 py-2 text-sm font-semibold text-white rounded transition-colors flex items-center gap-2 focus:outline-none"
                    style={{ background: accent }}
                  >
                    {loading ? <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" /> : null}
                    {buy ? "Buy" : "Sell"}
                  </button>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
