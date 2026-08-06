import { useEffect, useState } from "react";
import { X, AlignLeft, TrendingUp, Trash2, MoreHorizontal } from "lucide-react";
import { MarketDepth, wsUrl } from "../api";
import { C, FONT, inr } from "../lib/format";

interface MarketDepthModalProps {
  instrument: { instrument_key: string; symbol: string; exchange: string };
  mode: string;
  onClose: () => void;
  onTrade: (side: "BUY" | "SELL") => void;
}

export default function MarketDepthModal({ instrument, mode, onClose, onTrade }: MarketDepthModalProps) {
  const [depth, setDepth] = useState<MarketDepth | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout>;
    const key = encodeURIComponent(instrument.instrument_key.replace(/\|/g, ":"));

    const connect = () => {
      const ws = new WebSocket(wsUrl(`/ws/market-depth/${key}?mode=${mode}`));
      ws.onmessage = (event) => {
        if (cancelled) return;
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "depth") { setDepth(msg); setError(""); }
          else if (msg.type === "error") setError(msg.detail || "Failed to load market depth.");
        } catch (err) {
          console.error("Failed to parse /ws/market-depth message", err);
        }
      };
      ws.onclose = () => {
        if (!cancelled) reconnectTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
      return ws;
    };

    const ws = connect();
    return () => { cancelled = true; clearTimeout(reconnectTimer); ws.close(); };
  }, [instrument.instrument_key, mode]);

  const fmtTime = (raw: string | null) => {
    if (!raw) return "--";
    const ms = Number(raw);
    if (!Number.isFinite(ms)) return raw;
    return new Date(ms).toLocaleString("en-IN", { hour12: false });
  };

  const maxQty = depth ? Math.max(...depth.buy.map(l => l.qty), ...depth.sell.map(l => l.qty), 1) : 1;
  const totalBid = depth ? depth.buy.reduce((s, l) => s + l.qty, 0) : 0;
  const totalOffer = depth ? depth.sell.reduce((s, l) => s + l.qty, 0) : 0;
  const totalBidOrders = depth ? depth.buy.reduce((s, l) => s + l.orders, 0) : 0;
  const totalOfferOrders = depth ? depth.sell.reduce((s, l) => s + l.orders, 0) : 0;

  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center pt-24 bg-black/40 backdrop-blur-xs" style={FONT} onClick={onClose}>
      <div
        className="w-[520px] bg-white shadow-2xl rounded overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b" style={{ borderColor: C.border2 }}>
          <div className="flex items-center gap-2">
            <span className="text-[14px] font-bold" style={{ color: "#c2185b" }}>{instrument.symbol}</span>
            <span className="text-[10px] font-semibold text-gray-400 uppercase">{instrument.exchange}</span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => onTrade("BUY")}
              className="w-8 h-7 flex items-center justify-center text-xs font-bold rounded"
              style={{ background: C.buyBg, color: C.buyText }}
              title="Buy"
            >
              B
            </button>
            <button
              onClick={() => onTrade("SELL")}
              className="w-8 h-7 flex items-center justify-center text-xs font-bold rounded"
              style={{ background: C.sellBg, color: C.sellText }}
              title="Sell"
            >
              S
            </button>
            <button className="p-1.5 rounded text-gray-400 hover:text-gray-700 hover:bg-gray-100" title="Order book">
              <AlignLeft size={15} />
            </button>
            <button className="p-1.5 rounded text-gray-400 hover:text-gray-700 hover:bg-gray-100" title="Chart">
              <TrendingUp size={15} />
            </button>
            <button className="p-1.5 rounded text-gray-400 hover:text-gray-700 hover:bg-gray-100" title="Remove">
              <Trash2 size={15} />
            </button>
            <button className="p-1.5 rounded text-gray-400 hover:text-gray-700 hover:bg-gray-100" title="More">
              <MoreHorizontal size={15} />
            </button>
            <button onClick={onClose} className="p-1.5 ml-1 rounded text-gray-400 hover:text-gray-700 hover:bg-gray-100" title="Close">
              <X size={15} />
            </button>
          </div>
        </div>

        <div className="px-5 py-4">
          {error && (
            <div className="mb-4 bg-red-50 border border-red-200 text-red-600 px-4 py-2.5 rounded text-xs font-semibold">
              {error}
            </div>
          )}

          {/* Depth table */}
          <table className="w-full border-collapse text-[12px]">
            <thead>
              <tr style={{ color: C.muted }}>
                <Th>BID</Th>
                <Th right>ORDERS</Th>
                <Th right>QTY.</Th>
                <Th right className="pl-4" style={{ borderLeft: `1px solid ${C.border2}` }}>OFFER</Th>
                <Th right>ORDERS</Th>
                <Th right>QTY.</Th>
              </tr>
            </thead>
            <tbody>
              {[0, 1, 2, 3, 4].map((i) => {
                const bid = depth?.buy[i] ?? { price: 0, qty: 0, orders: 0 };
                const offer = depth?.sell[i] ?? { price: 0, qty: 0, orders: 0 };
                return (
                  <tr key={i} className="relative">
                    <Td>
                      <div className="relative">
                        <div
                          className="absolute inset-y-0 right-0"
                          style={{ width: `${(bid.qty / maxQty) * 100}%`, background: "rgba(76,175,80,0.08)" }}
                        />
                        <span className="relative font-mono" style={{ color: bid.price > 0 ? C.green : C.muted }}>{inr(bid.price)}</span>
                      </div>
                    </Td>
                    <Td right style={{ color: C.muted }}>{bid.orders}</Td>
                    <Td right className="font-mono">{bid.qty}</Td>
                    <Td right style={{ borderLeft: `1px solid ${C.border2}` }}>
                      <div className="relative">
                        <div
                          className="absolute inset-y-0 left-0"
                          style={{ width: `${(offer.qty / maxQty) * 100}%`, background: "rgba(223,81,76,0.08)" }}
                        />
                        <span className="relative font-mono" style={{ color: offer.price > 0 ? C.red : C.muted }}>{inr(offer.price)}</span>
                      </div>
                    </Td>
                    <Td right style={{ color: C.muted }}>{offer.orders}</Td>
                    <Td right className="font-mono">{offer.qty}</Td>
                  </tr>
                );
              })}
              <tr className="font-semibold border-t" style={{ borderColor: C.border2 }}>
                <Td style={{ color: C.green }}>Total</Td>
                <Td right style={{ color: C.muted }}>{totalBidOrders}</Td>
                <Td right className="font-mono" style={{ color: C.green }}>{totalBid}</Td>
                <Td right style={{ borderLeft: `1px solid ${C.border2}`, color: C.red }}>Total</Td>
                <Td right style={{ color: C.muted }}>{totalOfferOrders}</Td>
                <Td right className="font-mono" style={{ color: C.red }}>{totalOffer}</Td>
              </tr>
            </tbody>
          </table>

          {/* OHLC / circuit / volume grid */}
          <div className="grid grid-cols-2 gap-x-8 gap-y-2.5 mt-5 pt-4 border-t text-[13px]" style={{ borderColor: C.border2 }}>
            <StatRow label="Open" value={inr(depth?.ohlc.open ?? 0)} />
            <StatRow label="High" value={inr(depth?.ohlc.high ?? 0)} />
            <StatRow label="Low" value={inr(depth?.ohlc.low ?? 0)} />
            <StatRow label="Prev. Close" value={inr(depth?.ohlc.close ?? 0)} />
            <StatRow label="Volume" value={(depth?.volume ?? 0).toLocaleString("en-IN")} />
            <StatRow label="Avg. price" value={inr(depth?.average_price ?? 0)} />
            <StatRow label="Total buy qty" value={(depth?.total_buy_quantity ?? 0).toLocaleString("en-IN")} />
            <StatRow label="LTT" value={fmtTime(depth?.last_trade_time ?? null)} small />
            <StatRow label="Lower circuit" value={depth?.lower_circuit_limit != null ? inr(depth.lower_circuit_limit) : "--"} />
            <StatRow label="Upper circuit" value={depth?.upper_circuit_limit != null ? inr(depth.upper_circuit_limit) : "--"} />
          </div>
        </div>
      </div>
    </div>
  );
}

function Th({ children, right, className, style }: { children: React.ReactNode; right?: boolean; className?: string; style?: React.CSSProperties }) {
  return (
    <th
      className={`py-1.5 text-[10px] font-semibold uppercase tracking-wide ${right ? "text-right" : "text-left"} ${className || ""}`}
      style={style}
    >
      {children}
    </th>
  );
}

function Td({ children, right, className, style }: { children: React.ReactNode; right?: boolean; className?: string; style?: React.CSSProperties }) {
  return (
    <td className={`py-1 ${right ? "text-right" : "text-left"} ${className || ""}`} style={style}>
      {children}
    </td>
  );
}

function StatRow({ label, value, small }: { label: string; value: string; small?: boolean }) {
  return (
    <div className="flex justify-between">
      <span style={{ color: C.muted }}>{label}</span>
      <span className={`font-mono font-medium ${small ? "text-[11px]" : ""}`} style={{ color: C.text }}>{value}</span>
    </div>
  );
}
