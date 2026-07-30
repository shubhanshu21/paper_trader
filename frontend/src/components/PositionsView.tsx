import { useState } from "react";
import { Search, Download, BarChart2, TrendingUp, ChevronDown, ChevronUp } from "lucide-react";
import { OptionsPosition } from "../api";
import { C, FONT, inr, withSign, sign, Banner, Td, Th } from "./Common";

interface PositionsProps {
  openOptions: OptionsPosition[];
  onClosePosition: (id: number, type: "options" | "equity") => void;
  closingId: number | null;
}

interface PositionListItem {
  id?: number;
  product: string;
  symbol: string;
  exch: string;
  qty: number;
  avg: number;
  ltp: number;
  pnl: number;
  chg: number;
  isApi?: boolean;
}

const EmptyPositionsState = () => {
  return (
    <div className="flex flex-col items-center justify-center py-20 text-center bg-white border border-dashed rounded-lg p-10 select-none">
      <svg viewBox="0 0 120 120" className="w-24 h-24 mb-6 opacity-80" style={{ fill: "none" }}>
        <circle cx="60" cy="50" r="30" stroke="#e2e8f0" strokeWidth="2" />
        <path d="M45,85 L75,85 C80,85 85,90 85,95 L85,110 L35,110 L35,95 C35,90 40,85 45,85 Z" fill="#f8fafc" stroke="#cbd5e1" strokeWidth="2" />
        <rect x="50" y="40" width="20" height="20" rx="3" fill="#ffffff" stroke="#ff5722" strokeWidth="1.5" />
        <circle cx="60" cy="50" r="4" fill="#ff5722" />
      </svg>
      <h3 className="text-[18px] font-normal text-gray-700 mb-2">You don't have any positions yet</h3>
      <p className="text-[13px] text-gray-400 max-w-sm mb-6">
        Check out active strategies or place manual trades from the watchlist to open a position.
      </p>
      <button 
        className="px-6 py-2.5 text-xs font-bold text-white rounded bg-orange-500 hover:bg-orange-600 transition-colors shadow-sm focus:outline-none"
        style={{ backgroundColor: C.orange }}
      >
        Get started
      </button>
    </div>
  );
};

export default function PositionsView({ openOptions, onClosePosition, closingId }: PositionsProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [hoveredSymbol, setHoveredSymbol] = useState<string | null>(null);
  const [historyExpanded, setHistoryExpanded] = useState(false);

  // Map API options positions
  const apiPositionsList: PositionListItem[] = openOptions.map(pos => ({
    id: pos.id,
    product: pos.product || "NRML",
    symbol: pos.symbol,
    exch: "NFO",
    qty: pos.quantity,
    avg: (pos.call_entry_price + pos.put_entry_price) / 2,
    ltp: (pos.call_strike + pos.put_strike) / 2,
    pnl: pos.mtm || 0.00,
    chg: 0.00,
    isApi: true,
  }));

  const filteredPositions = apiPositionsList.filter(p => 
    p.symbol.toLowerCase().includes(searchQuery.toLowerCase())
  );

  // Calculate totals
  const totalPL = filteredPositions.reduce((sum, p) => sum + p.pnl, 0);

  // Determine max P&L for breakdown bar chart scaling
  const maxPnlAbs = Math.max(...filteredPositions.map(p => Math.abs(p.pnl)), 1);

  const handleClose = (item: PositionListItem) => {
    if (item.isApi && item.id) {
      onClosePosition(item.id, "options");
    }
  };

  return (
    <div className="w-full animate-fade-in" style={FONT}>
      <Banner />

      {apiPositionsList.length === 0 ? (
        <EmptyPositionsState />
      ) : (
        <>
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-5">
            <h2 className="text-[17px] font-semibold text-gray-700">Positions ({filteredPositions.length})</h2>

            <div className="flex items-center gap-4 text-xs">
              <div
                className="flex items-center gap-2 px-3 py-1.5 rounded bg-white border"
                style={{ borderColor: C.border2 }}
              >
                <Search size={13} style={{ color: C.faint }} />
                <input 
                  placeholder="Search" 
                  className="text-xs outline-none w-28 bg-transparent text-gray-700 font-medium" 
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                />
              </div>
              <button className="flex items-center gap-1 text-blue-500 font-semibold hover:underline">
                <TrendingUp size={13} /> Analyze
              </button>
              <button className="flex items-center gap-1 text-blue-500 font-semibold hover:underline">
                <BarChart2 size={13} /> Analytics
              </button>
              <button className="flex items-center gap-1 text-blue-500 font-semibold hover:underline">
                <Download size={13} /> Download
              </button>
            </div>
          </div>

          {filteredPositions.length === 0 ? (
            <div className="py-16 text-center text-gray-400 border border-dashed rounded bg-gray-50 text-xs mb-10">
              No positions match your search query.
            </div>
          ) : (
            <>
              {/* Table */}
              <div className="overflow-x-auto bg-white rounded border shadow-xs mb-10" style={{ borderColor: C.tableBorder }}>
                <table className="w-full border-collapse">
                  <thead>
                    <tr className="border-b text-xs" style={{ borderColor: C.border2, background: C.tableHeaderBg }}>
                      <th className="px-4 py-3 text-left w-12">
                        <input type="checkbox" className="rounded border-gray-300 text-orange-500 focus:ring-orange-500" disabled />
                      </th>
                      <Th>Product</Th>
                      <Th>Instrument</Th>
                      <Th right>Qty.</Th>
                      <Th right>Avg.</Th>
                      <Th right>LTP</Th>
                      <Th right>P&amp;L</Th>
                      <Th right className="w-28">Chg.</Th>
                    </tr>
                  </thead>
                  <tbody className="divide-y" style={{ borderColor: C.border }}>
                    {filteredPositions.map((o) => {
                      const isClosing = closingId === o.id;
                      const isHovered = hoveredSymbol === o.symbol;
                      const isShort = o.qty < 0;

                      return (
                        <tr 
                          key={o.symbol}
                          onMouseEnter={() => setHoveredSymbol(o.symbol)}
                          onMouseLeave={() => setHoveredSymbol(null)}
                          className="hover:bg-gray-50 transition-colors"
                          style={{ height: "40px" }}
                        >
                          <td className="px-4 py-3 text-left w-12">
                            <input type="checkbox" className="rounded border-gray-300 text-orange-500 focus:ring-orange-500" />
                          </td>
                          <Td>
                            {o.product === "CNC" ? (
                              <span className="px-2 py-0.5 text-[10px] rounded font-bold bg-[#fff9e9] text-[#b58900]">
                                CNC
                              </span>
                            ) : (
                              <span className="px-2 py-0.5 text-[10px] rounded font-bold bg-gray-100 text-gray-500">
                                {o.product}
                              </span>
                            )}
                          </Td>
                          <Td className="font-semibold text-gray-700">
                            <div className="flex items-baseline gap-1.5 text-[13px]">
                              <span>{o.symbol}</span>
                              <span className="text-[9px] text-gray-400 uppercase font-semibold">{o.exch}</span>
                            </div>
                          </Td>
                          <Td right style={{ color: isShort ? C.red : C.blue }} className="font-semibold">
                            {o.qty}
                          </Td>
                          <Td right className="text-gray-700 font-mono">{inr(o.avg)}</Td>
                          <Td right className="text-gray-700 font-mono">{inr(o.ltp)}</Td>
                          <Td right style={{ color: sign(o.pnl) }} className="font-mono font-medium">
                            {withSign(o.pnl)}
                          </Td>
                          {/* Chg overlaid with Square Off exit on hover */}
                          <Td right className="w-28 text-right">
                            {isHovered ? (
                              <button
                                onClick={() => handleClose(o)}
                                disabled={isClosing}
                                className="px-2 py-0.5 text-[11px] font-bold text-white rounded bg-red-500 hover:bg-red-600 disabled:bg-gray-300 transition-colors shadow-sm focus:outline-none"
                              >
                                {isClosing ? "..." : "Square Off"}
                              </button>
                            ) : (
                              <span style={{ color: sign(o.chg) }} className="font-mono font-medium">
                                {withSign(o.chg)}%
                              </span>
                            )}
                          </Td>
                        </tr>
                      );
                    })}
                    <tr className="bg-gray-50 font-semibold border-t text-[13px]" style={{ borderColor: C.border2 }}>
                      <Td colSpan={5} className="text-gray-500 font-semibold">Total</Td>
                      <Td />
                      <Td right style={{ color: sign(totalPL) }} className="font-mono font-bold">
                        {withSign(totalPL)}
                      </Td>
                      <Td />
                    </tr>
                  </tbody>
                </table>
              </div>

              {/* Collapsible Day's History */}
              <div className="border-t pt-4 mb-8" style={{ borderColor: C.border2 }}>
                <button
                  onClick={() => setHistoryExpanded(!historyExpanded)}
                  className="flex items-center gap-2 text-[14px] font-semibold text-gray-700 hover:text-orange-500 focus:outline-none"
                >
                  <span>Day's history (0)</span>
                  {historyExpanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
                </button>
                {historyExpanded && (
                  <div className="mt-4 py-8 text-center text-xs text-gray-400 border border-dashed rounded bg-gray-50 animate-fade-in">
                    No past positions squared off today. All trades represent active open exposures.
                  </div>
                )}
              </div>

              {/* Breakdown Section (CSS bi-directional bar chart matching screenshot) */}
              <div className="space-y-4 pt-4 border-t" style={{ borderColor: C.border2 }}>
                <h3 className="text-[14px] font-semibold text-gray-700">Breakdown</h3>
                <div className="space-y-3 bg-white p-6 border rounded shadow-xs max-w-2xl">
                  {filteredPositions.map((pos) => {
                    const isPositive = pos.pnl > 0;
                    const percent = Math.min((Math.abs(pos.pnl) / maxPnlAbs) * 100, 100);

                    return (
                      <div key={pos.symbol} className="flex items-center text-xs">
                        {/* Label */}
                        <div className="w-48 text-gray-500 font-medium truncate pr-4">
                          {pos.symbol} <span className="text-[10px] text-gray-400">({pos.product})</span>
                        </div>

                        {/* Bi-directional Bar chart */}
                        <div className="flex-1 flex h-4 items-center bg-gray-50/50 rounded-xs relative">
                          {/* Left Half (Negative P&L) */}
                          <div className="flex-1 flex justify-end pr-[1px]">
                            {!isPositive && pos.pnl !== 0 && (
                              <div 
                                className="h-2.5 rounded-l-xs"
                                style={{ 
                                  width: `${percent}%`, 
                                  background: C.red,
                                  transition: "width 0.3s ease"
                                }} 
                              />
                            )}
                          </div>

                          {/* Center Axis Line */}
                          <div className="w-[1.5px] h-3.5 bg-gray-300 absolute left-1/2 transform -translate-x-1/2" />

                          {/* Right Half (Positive P&L) */}
                          <div className="flex-1 flex justify-start pl-[1px]">
                            {isPositive && (
                              <div 
                                className="h-2.5 rounded-r-xs"
                                style={{ 
                                  width: `${percent}%`, 
                                  background: C.blue,
                                  transition: "width 0.3s ease" 
                                }} 
                              />
                            )}
                          </div>
                        </div>

                        {/* Value readout */}
                        <div className="w-20 text-right font-mono font-semibold pl-4" style={{ color: sign(pos.pnl) }}>
                          {withSign(pos.pnl)}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
