import { useState } from "react";
import { Search, Download, BarChart2, Users, ChevronDown } from "lucide-react";
import { EquityPosition } from "../api";
import { C, FONT, inr, withSign, sign, Banner, Td, Th } from "./Common";

interface HoldingsProps {
  openEquity: EquityPosition[];
  closedEquity: EquityPosition[];
  ltps: { [key: string]: number };
  onClosePosition: (id: number, type: "options" | "equity") => void;
  closingId: number | null;
}

interface HoldingListItem {
  id?: number;
  symbol: string;
  name: string;
  qty: number;
  avgCost: number;
  ltp: number;
  dayChg: number;
  isApi?: boolean;
}

const EmptyHoldingsState = () => {
  return (
    <div className="flex flex-col items-center justify-center py-20 text-center bg-white border border-dashed rounded-lg p-10 select-none">
      <svg viewBox="0 0 120 120" className="w-24 h-24 mb-6 opacity-80" style={{ fill: "none" }}>
        <circle cx="60" cy="50" r="30" stroke="#e2e8f0" strokeWidth="2" />
        <path d="M45,85 L75,85 C80,85 85,90 85,95 L85,110 L35,110 L35,95 C35,90 40,85 45,85 Z" fill="#f8fafc" stroke="#cbd5e1" strokeWidth="2" />
        <circle cx="60" cy="50" r="12" fill="#ffffff" stroke="#ff5722" strokeWidth="2" />
        <path d="M60,44 L60,56 M54,50 L66,50" stroke="#ff5722" strokeWidth="2" strokeLinecap="round" />
      </svg>
      <h3 className="text-[18px] font-normal text-gray-700 mb-2">You haven't invested in anything yet</h3>
      <p className="text-[13px] text-gray-400 max-w-sm mb-6">
        To get started, search for stocks in the watchlist and place an order.
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

export default function HoldingsView({ openEquity, ltps, onClosePosition, closingId }: HoldingsProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [hoveredSymbol, setHoveredSymbol] = useState<string | null>(null);

  // Map API open positions
  const apiHoldingsList: HoldingListItem[] = openEquity.map(pos => {
    const sym = pos.display_symbol || pos.symbol.split("|")[0];
    return {
      id: pos.id,
      symbol: sym,
      name: pos.symbol,
      qty: pos.quantity,
      avgCost: pos.entry_price,
      ltp: ltps[pos.symbol] || pos.entry_price,
      dayChg: 0.00, // API doesn't specify day change, set standard baseline
      isApi: true,
    };
  });

  const filteredHoldings = apiHoldingsList.filter(h => 
    h.symbol.toLowerCase().includes(searchQuery.toLowerCase()) ||
    h.name.toLowerCase().includes(searchQuery.toLowerCase())
  );

  // Dynamic aggregates
  const totalInvestment = filteredHoldings.reduce((sum, h) => sum + (h.avgCost * h.qty), 0);
  const totalCurrentValue = filteredHoldings.reduce((sum, h) => sum + (h.ltp * h.qty), 0);
  const totalPL = totalCurrentValue - totalInvestment;
  const totalPLPct = totalInvestment > 0 ? (totalPL / totalInvestment) * 100 : 0;

  // Day's P&L calculation: sum of (qty * ltp * (dayChg / 100))
  const totalDayPL = filteredHoldings.reduce((sum, h) => {
    const ltpVal = h.ltp;
    const itemDayPL = h.qty * ltpVal * (h.dayChg / 100);
    return sum + itemDayPL;
  }, 0);
  const totalDayPLPct = totalInvestment > 0 ? (totalDayPL / totalInvestment) * 100 : 0;

  const handleClose = (item: HoldingListItem) => {
    if (item.isApi && item.id) {
      onClosePosition(item.id, "equity");
    }
  };

  return (
    <div className="w-full animate-fade-in" style={FONT}>
      <Banner />

      {apiHoldingsList.length === 0 ? (
        <EmptyHoldingsState />
      ) : (
        <>
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-6">
            <div className="flex items-center gap-3">
              <h2 className="text-[17px] font-semibold text-gray-700">Holdings ({filteredHoldings.length})</h2>
              <div className="relative">
                <select className="appearance-none bg-gray-50 border rounded text-xs px-3 py-1 pr-6 font-semibold text-gray-500 cursor-pointer focus:outline-none focus:ring-1 focus:ring-orange-500">
                  <option>All stocks</option>
                </select>
                <ChevronDown size={11} className="absolute right-2 top-2 text-gray-400 pointer-events-none" />
              </div>
            </div>

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
                <Users size={13} /> Family
              </button>
              <button className="flex items-center gap-1 text-blue-500 font-semibold hover:underline">
                <BarChart2 size={13} /> Analytics
              </button>
              <button className="flex items-center gap-1 text-blue-500 font-semibold hover:underline">
                <Download size={13} /> Download
              </button>
            </div>
          </div>

          {/* Metrics Row */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-6 p-5 bg-slate-50 border rounded shadow-xs mb-8">
            {[
              ["Total Investment", inr(totalInvestment), "00", C.text],
              ["Current value", inr(totalCurrentValue), "00", C.text],
              ["Day's P&L", withSign(totalDayPL), `(${withSign(totalDayPLPct)}%)`, sign(totalDayPL)],
              ["Total P&L", inr(totalPL), `(${withSign(totalPLPct)}%)`, sign(totalPL)],
            ].map(([l, big, small, col], i) => (
              <div key={i} className="flex flex-col">
                <span className="text-[11px] text-gray-400 font-semibold mb-1 uppercase tracking-wider">{l}</span>
                <div className="text-[22px] font-light flex items-baseline gap-1" style={{ color: col as string }}>
                  ₹{big}
                  <span className="text-[11px] font-semibold">{small}</span>
                </div>
              </div>
            ))}
          </div>

          {/* Table */}
          {filteredHoldings.length === 0 ? (
            <div className="py-16 text-center text-gray-400 border border-dashed rounded bg-gray-50 text-xs">
              No holdings match your search query.
            </div>
          ) : (
            <div className="overflow-x-auto bg-white rounded border shadow-xs" style={{ borderColor: C.tableBorder }}>
              <table className="w-full border-collapse">
                <thead>
                  <tr className="border-b text-xs" style={{ borderColor: C.border2, background: C.tableHeaderBg }}>
                    <Th>Instrument</Th>
                    <Th right>Qty.</Th>
                    <Th right>Avg. cost</Th>
                    <Th right>LTP</Th>
                    <Th right>Cur. val</Th>
                    <Th right>P&amp;L</Th>
                    <Th right>Net chg.</Th>
                    <Th right className="w-24">Day chg.</Th>
                  </tr>
                </thead>
                <tbody className="divide-y" style={{ borderColor: C.border }}>
                  {filteredHoldings.map((h) => {
                    const curVal = h.ltp * h.qty;
                    const invVal = h.avgCost * h.qty;
                    const pnl = curVal - invVal;
                    const chg = invVal > 0 ? (pnl / invVal) * 100 : 0;
                    const isClosing = closingId === h.id;
                    const isHovered = hoveredSymbol === h.symbol;

                    return (
                      <tr 
                        key={h.symbol} 
                        onMouseEnter={() => setHoveredSymbol(h.symbol)}
                        onMouseLeave={() => setHoveredSymbol(null)}
                        className="hover:bg-gray-50 transition-colors"
                        style={{ height: "40px" }}
                      >
                        {/* Instrument */}
                        <Td style={{ color: C.blue }} className="font-semibold cursor-pointer">
                          <div className="flex flex-col">
                            <span>{h.symbol}</span>
                            {h.isApi && <span className="text-[9px] text-gray-400 font-mono tracking-tighter truncate max-w-xs">{h.name}</span>}
                          </div>
                        </Td>
                        {/* Qty */}
                        <Td right className="text-gray-700 font-medium">{h.qty}</Td>
                        {/* Avg. cost */}
                        <Td right className="text-gray-700 font-mono">{inr(h.avgCost)}</Td>
                        {/* LTP */}
                        <Td right className="text-gray-700 font-mono">{inr(h.ltp)}</Td>
                        {/* Cur val */}
                        <Td right className="text-gray-700 font-mono font-medium">{inr(curVal)}</Td>
                        {/* P&L */}
                        <Td right style={{ color: sign(pnl) }} className="font-mono font-medium">
                          {withSign(pnl)}
                        </Td>
                        {/* Net chg */}
                        <Td right style={{ color: sign(chg) }} className="font-mono font-medium">
                          {withSign(chg)}%
                        </Td>
                        {/* Day chg (overlaid with Exit button on hover) */}
                        <Td right className="w-24 text-right">
                          {isHovered ? (
                            <button
                              onClick={() => handleClose(h)}
                              disabled={isClosing}
                              className="px-3 py-1 text-[11px] font-bold text-white rounded bg-red-500 hover:bg-red-600 disabled:bg-gray-300 transition-colors shadow-sm focus:outline-none"
                            >
                              {isClosing ? "..." : "Exit"}
                            </button>
                          ) : (
                            <span style={{ color: sign(h.dayChg) }} className="font-mono font-medium">
                              {withSign(h.dayChg)}%
                            </span>
                          )}
                        </Td>
                      </tr>
                    );
                  })}
                  
                  {/* Total row matching screenshot */}
                  <tr className="bg-gray-50 font-semibold border-t text-[13px]" style={{ borderColor: C.border2 }}>
                    <Td colSpan={4} className="text-gray-500 font-semibold">Total</Td>
                    <Td right className="font-mono font-bold text-gray-700">{inr(totalCurrentValue)}</Td>
                    <Td right style={{ color: sign(totalPL) }} className="font-mono font-bold">
                      {inr(totalPL)}
                    </Td>
                    <Td right style={{ color: sign(totalPLPct) }} className="font-mono font-bold">
                      {withSign(totalPLPct)}%
                    </Td>
                    <Td right style={{ color: sign(totalDayPLPct) }} className="font-mono font-bold">
                      {withSign(totalDayPLPct)}%
                    </Td>
                  </tr>
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
