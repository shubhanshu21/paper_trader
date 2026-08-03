import { useState } from "react";
import { Search, Download, Clock, FileText, ChevronDown, ChevronUp } from "lucide-react";
import { Order } from "../api";
import { StatusPill, TypeTag, Td, Th } from "./Common";
import { C, FONT, inr } from "../lib/format";

interface OrdersProps {
  orders: Order[];
}

interface OrderListItem {
  time: string;
  type: "BUY" | "SELL";
  symbol: string;
  exch: string;
  product: string;
  qty: string;
  price: number;
  status: string;
}

const EmptyState = () => {
  const item = {
    title: "You haven't placed any orders today",
    desc: "Play the game with custom rules or choose pre-built ones.",
    btn: "Get started"
  };

  return (
    <div className="flex flex-col items-center justify-center py-20 text-center bg-white border border-dashed rounded-lg p-10 select-none">
      <svg viewBox="0 0 120 120" className="w-24 h-24 mb-6 opacity-80" style={{ fill: "none" }}>
        <circle cx="60" cy="50" r="30" stroke="#e2e8f0" strokeWidth="2" />
        <path d="M45,85 L75,85 C80,85 85,90 85,95 L85,110 L35,110 L35,95 C35,90 40,85 45,85 Z" fill="#f8fafc" stroke="#cbd5e1" strokeWidth="2" />
        <rect x="52" y="42" width="16" height="16" rx="2" fill="#ffffff" stroke="#cbd5e1" strokeWidth="1.5" />
        <line x1="56" y1="47" x2="64" y2="47" stroke="#ff5722" strokeWidth="1.5" />
        <line x1="56" y1="51" x2="64" y2="51" stroke="#94a3b8" strokeWidth="1.5" />
        <line x1="56" y1="55" x2="61" y2="55" stroke="#cbd5e1" strokeWidth="1.5" />
      </svg>
      <h3 className="text-[18px] font-normal text-gray-700 mb-2">{item.title}</h3>
      <p className="text-[13px] text-gray-400 max-w-sm mb-6">{item.desc}</p>
      <button 
        className="px-6 py-2.5 text-xs font-bold text-white rounded bg-orange-500 hover:bg-orange-600 transition-colors shadow-sm focus:outline-none"
        style={{ backgroundColor: C.orange }}
      >
        {item.btn}
      </button>
    </div>
  );
};

export default function OrdersView({ orders }: OrdersProps) {
  const [openQuery, setOpenQuery] = useState("");
  const [execQuery, setExecQuery] = useState("");
  const [tradesExpanded, setTradesExpanded] = useState(false);

  // Map API orders to visual list items
  const mappedOrders: OrderListItem[] = orders.map(o => {
    const timePart = o.date.includes(" ") 
      ? o.date.split(" ")[1] 
      : o.date.includes("T") 
        ? o.date.split("T")[1].substring(0, 8) 
        : o.date;

    return {
      time: timePart || "12:00:00",
      type: o.transaction_type as "BUY" | "SELL",
      symbol: o.display_symbol || o.symbol.split("|")[0],
      exch: o.symbol.includes("FUT") || o.symbol.includes("CE") || o.symbol.includes("PE") ? "NFO" : "NSE",
      product: o.strategy_name === "manual_trade" ? "CNC" : "MIS",
      qty: `${o.quantity} / ${o.quantity}`,
      price: o.price,
      status: o.status,
    };
  });

  const openOrdersList = mappedOrders.filter(o => 
    o.status === "OPEN" || o.status === "PENDING" || o.status === "TRIGGER PENDING"
  );
  
  const executedOrdersList = mappedOrders.filter(o => 
    o.status !== "OPEN" && o.status !== "PENDING" && o.status !== "TRIGGER PENDING"
  );

  const filteredOpen = openOrdersList.filter(o => 
    o.symbol.toLowerCase().includes(openQuery.toLowerCase())
  );

  const filteredExec = executedOrdersList.filter(o => 
    o.symbol.toLowerCase().includes(execQuery.toLowerCase())
  );

  return (
    <div className="w-full" style={FONT}>
      {/* Sub tabs */}
      <div className="flex gap-8 border-b text-xs pb-3 mb-6" style={{ borderColor: C.border2 }}>
        <button
          className="font-semibold transition-colors focus:outline-none relative text-xs"
          style={{ color: C.orange }}
        >
          Orders
          <span
            className="absolute bottom-[-13px] left-0 right-0 h-[2px]"
            style={{ background: C.orange }}
          />
        </button>
      </div>

      {orders.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="space-y-10">
          {/* Open Orders Section */}
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-[15px] font-semibold text-gray-700">Open orders ({filteredOpen.length})</h2>

              <div className="flex items-center gap-4 text-xs">
                <div
                  className="flex items-center gap-2 px-3 py-1.5 rounded bg-white border"
                  style={{ borderColor: C.border2 }}
                >
                  <Search size={13} style={{ color: C.faint }} />
                  <input
                    placeholder="Search"
                    className="outline-none w-28 bg-transparent text-xs text-gray-700"
                    value={openQuery}
                    onChange={(e) => setOpenQuery(e.target.value)}
                  />
                </div>
                <button className="flex items-center gap-1 text-blue-500 font-semibold hover:underline">
                  <Download size={13} /> Download
                </button>
              </div>
            </div>

            {filteredOpen.length === 0 ? (
              <div className="py-10 text-center text-gray-400 border border-dashed rounded-lg bg-gray-50 text-xs">
                No matching open orders.
              </div>
            ) : (
              <div className="bg-white border rounded shadow-xs overflow-hidden" style={{ borderColor: C.tableBorder }}>
                <table className="w-full border-collapse">
                  <thead>
                    <tr className="border-b text-xs" style={{ borderColor: C.border2, background: C.tableHeaderBg }}>
                      <th className="px-4 py-3 text-left w-12">
                        <input type="checkbox" className="rounded border-gray-300 text-orange-500 focus:ring-orange-500" disabled />
                      </th>
                      <Th>Time</Th>
                      <Th>Type</Th>
                      <Th>Instrument</Th>
                      <Th>Product</Th>
                      <Th right>Qty.</Th>
                      <Th right>Price</Th>
                      <Th right>Status</Th>
                    </tr>
                  </thead>
                  <tbody className="divide-y" style={{ borderColor: C.border }}>
                    {filteredOpen.map((o, idx) => (
                      <tr key={idx} className="hover:bg-gray-50 transition-colors" style={{ height: "40px" }}>
                        <td className="px-4 py-3 text-left w-12">
                          <input type="checkbox" className="rounded border-gray-300 text-orange-500 focus:ring-orange-500" />
                        </td>
                        <Td className="text-gray-500 font-mono">{o.time}</Td>
                        <Td>
                          <TypeTag t={o.type} />
                        </Td>
                        <Td>
                          <div className="flex items-baseline gap-1 text-[13px]">
                            <span className="font-semibold text-gray-700">{o.symbol}</span>
                            <span className="text-[10px] text-gray-400 font-medium uppercase">{o.exch}</span>
                          </div>
                        </Td>
                        <Td className="text-gray-500 font-semibold">{o.product}</Td>
                        <Td right className="font-semibold text-gray-700">{o.qty}</Td>
                        <Td right className="font-semibold text-gray-700 font-mono">{inr(o.price)}</Td>
                        <Td right>
                          <StatusPill status={o.status} />
                        </Td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Executed Orders Section */}
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-[15px] font-semibold text-gray-700">Executed orders ({filteredExec.length})</h2>

              <div className="flex items-center gap-4 text-xs">
                <div
                  className="flex items-center gap-2 px-3 py-1.5 rounded bg-white border"
                  style={{ borderColor: C.border2 }}
                >
                  <Search size={13} style={{ color: C.faint }} />
                  <input
                    placeholder="Search"
                    className="outline-none w-28 bg-transparent text-xs text-gray-700"
                    value={execQuery}
                    onChange={(e) => setExecQuery(e.target.value)}
                  />
                </div>
                <button className="flex items-center gap-1 text-blue-500 font-semibold hover:underline">
                  <FileText size={13} /> Contract note
                </button>
                <button className="flex items-center gap-1 text-blue-500 font-semibold hover:underline">
                  <Clock size={13} /> View history
                </button>
                <button className="flex items-center gap-1 text-blue-500 font-semibold hover:underline">
                  <Download size={13} /> Download
                </button>
              </div>
            </div>

            {filteredExec.length === 0 ? (
              <div className="py-10 text-center text-gray-400 border border-dashed rounded-lg bg-gray-50 text-xs">
                No matching executed orders.
              </div>
            ) : (
              <div className="bg-white border rounded shadow-xs overflow-hidden" style={{ borderColor: C.tableBorder }}>
                <table className="w-full border-collapse">
                  <thead>
                    <tr className="border-b text-xs" style={{ borderColor: C.border2, background: C.tableHeaderBg }}>
                      <Th>Time</Th>
                      <Th>Type</Th>
                      <Th>Instrument</Th>
                      <Th>Product</Th>
                      <Th right>Qty.</Th>
                      <Th right>Avg. price</Th>
                      <Th right>Status</Th>
                    </tr>
                  </thead>
                  <tbody className="divide-y" style={{ borderColor: C.border }}>
                    {filteredExec.map((o, idx) => (
                      <tr key={idx} className="hover:bg-gray-50 transition-colors" style={{ height: "40px" }}>
                        <Td className="text-gray-500 font-mono">{o.time}</Td>
                        <Td>
                          <TypeTag t={o.type} />
                        </Td>
                        <Td>
                          <div className="flex items-baseline gap-1 text-[13px]">
                            <span className="font-semibold text-gray-700">{o.symbol}</span>
                            <span className="text-[10px] text-gray-400 font-medium uppercase">{o.exch}</span>
                          </div>
                        </Td>
                        <Td className="text-gray-500 font-semibold">{o.product}</Td>
                        <Td right className="font-semibold text-gray-700">{o.qty}</Td>
                        <Td right className="font-semibold text-gray-700 font-mono">₹{inr(o.price)}</Td>
                        <Td right>
                          <StatusPill status={o.status} />
                        </Td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Collapsible Trades Section */}
          <div className="border-t pt-4" style={{ borderColor: C.border2 }}>
            <button
              onClick={() => setTradesExpanded(!tradesExpanded)}
              className="flex items-center gap-2 text-[14px] font-semibold text-gray-700 hover:text-orange-500 focus:outline-none"
            >
              <span>Trades</span>
              {tradesExpanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
            </button>
            {tradesExpanded && (
              <div className="mt-4 py-8 text-center text-xs text-gray-400 border border-dashed rounded-lg bg-gray-50 animate-fade-in">
                No standalone trade executions. Open order logs represent single complete fills.
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
