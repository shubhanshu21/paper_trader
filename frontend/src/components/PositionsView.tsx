import { useState, ReactNode } from "react";
import { Search, Download, BarChart2, TrendingUp, ChevronDown, ChevronUp } from "lucide-react";
import { OptionsPosition, EquityPosition } from "../api";
import { Banner, Td, Th } from "./Common";
import { C, FONT, inr, withSign, sign } from "../lib/format";
import { useCustomStrategyPositions as useLiveCustomPositions } from "../hooks/useCustomStrategyPositions";

function ordinalSuffix(day: number): string {
  if (day >= 11 && day <= 13) return "th";
  switch (day % 10) {
    case 1: return "st";
    case 2: return "nd";
    case 3: return "rd";
    default: return "th";
  }
}

const getTodayIso = () => {
  const d = new Date();
  // Shift to IST (UTC + 5:30) since the trading market operates on IST
  const istOffset = 5.5 * 60 * 60 * 1000;
  const istDate = new Date(d.getTime() + istOffset);
  return istDate.toISOString().slice(0, 10);
};

// Kite's compact options-instrument label: expiry day-ordinal, a small
// "W"/"M" weekly/monthly badge, then "DDMon strike TYPE" — e.g. "25th ᵂ
// 25AUG 1430 CE". Built from real data (strike/expiry/expiry_mode), not
// guessed — expiry_mode comes straight from the strategy's own rules.
function kiteOptionLabel(strike: number, optionType: string, expiryIso: string, expiryMode: "WEEKLY" | "MONTHLY"): ReactNode {
  const d = new Date(expiryIso);
  const day = d.getDate();
  const mon = d.toLocaleDateString("en-GB", { month: "short" }).toUpperCase();
  return (
    <span className="inline-flex items-baseline gap-1">
      <span>
        {day}<sup className="text-[9px]">{ordinalSuffix(day)}</sup>{" "}
        <sup className="text-[9px] font-bold text-gray-400">{expiryMode === "WEEKLY" ? "W" : "M"}</sup>
      </span>
      <span>{day}{mon} {strike} {optionType}</span>
    </span>
  );
}

function useCustomStrategyPositions(): PositionListItem[] {
  const rows = useLiveCustomPositions();

  // Same PositionListItem shape the legacy table already renders, so
  // custom-strategy legs sit as ordinary rows in ONE unified table
  // (Product/Instrument/Qty/Avg/LTP/P&L/Chg) rather than a separate
  // section — an arbitrary-leg custom strategy has no natural "one row
  // per basket" shape anyway, so per-leg rows here is the honest fit.
  return rows.map((r) => ({
    id: r.id,
    // Real margin product (matches how these are actually placed — see
    // rule_strategy.py's default "NRML") — the strategy name itself is
    // shown on the Strategies page's own Positions panel, not repeated here.
    product: "NRML",
    // Plain string kept for search/filter matching (Instrument search box)
    // and the React list key — the fancy ordinal/weekly-badge rendering
    // below is a separate displaySymbol override, JSX can't be searched.
    symbol: r.strike != null
      ? `${r.symbol} ${r.expiry ? new Date(r.expiry).toLocaleDateString("en-GB", { day: "2-digit", month: "short" }).toUpperCase() : ""} ${r.strike} ${r.option_type}`.trim()
      : r.symbol,
    displaySymbol: r.strike != null && r.expiry && r.option_type
      ? kiteOptionLabel(r.strike, r.option_type, r.expiry, r.expiry_mode)
      : undefined,
    exch: r.instrument_type === "EQUITY" ? "NSE" : "NFO",
    qty: r.transaction_type === "SELL" ? -r.quantity : r.quantity,
    avg: r.entry_price,
    ltp: r.ltp ?? r.entry_price,
    pnl: r.pnl ?? 0,
    pnlUnknown: r.pnl == null,
    chg: r.chg_pct ?? 0,
    isApi: false,
  }));
}

interface PositionsProps {
  openOptions: OptionsPosition[];
  openEquity: EquityPosition[];
  ltps: { [key: string]: number };
  onClosePosition: (id: number, type: "options" | "equity" | "custom") => void;
  // Prefixed by source table ('option-7'/'equity-7'/'custom-7') — ids
  // aren't unique across the three underlying tables, see dataSlice.ts.
  closingId: string | null;
}

interface PositionListItem {
  id?: number;
  product: string;
  symbol: string;
  displaySymbol?: ReactNode;
  exch: string;
  qty: number;
  avg: number;
  ltp: number;
  pnl: number;
  // True when the real P&L isn't known yet (server hasn't sent a value)
  // rather than the position genuinely being at breakeven — lets the P&L
  // cell show "—" instead of a misleading "₹0.00" for a split second
  // right after load, before the live /ws/positions feed's first tick.
  pnlUnknown?: boolean;
  chg: number;
  isApi?: boolean;
  isEquity?: boolean;
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

export default function PositionsView({ openOptions, openEquity, ltps, onClosePosition, closingId }: PositionsProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [hoveredRowKey, setHoveredRowKey] = useState<string | null>(null);
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const customRows = useCustomStrategyPositions();

  // Map API options positions
  const legacyPositionsList: PositionListItem[] = openOptions.map(pos => ({
    id: pos.id,
    product: pos.product || "NRML",
    symbol: pos.symbol,
    exch: "NFO",
    qty: pos.quantity,
    avg: (pos.call_entry_price + pos.put_entry_price) / 2,
    // Average of the two legs' REAL current prices (server-computed
    // mark-to-market inputs), not the strikes — strikes are fixed at
    // entry and never move, so averaging them produced a static number
    // with no relationship to the market. Falls back to the entry-price
    // average (last-known, not live) only while call_ltp/put_ltp haven't
    // loaded yet, rather than showing the misleading strike average.
    ltp: (pos.call_ltp != null && pos.put_ltp != null)
      ? (pos.call_ltp + pos.put_ltp) / 2
      : (pos.call_entry_price + pos.put_entry_price) / 2,
    pnl: pos.mtm ?? 0.00,
    pnlUnknown: pos.mtm == null,
    chg: 0.00,
    isApi: true,
    isEquity: false,
  }));

  // Map API open equity positions that are either MIS or CNC bought today
  const todayStr = getTodayIso();
  const todayEquityPositions: PositionListItem[] = openEquity
    .filter(pos => pos.product === "MIS" || (pos.product === "CNC" && pos.entry_date === todayStr))
    .map(pos => {
      const sym = pos.display_symbol || pos.symbol.split("|")[0];
      return {
        id: pos.id,
        product: pos.product || "CNC",
        symbol: sym,
        exch: "NSE",
        // Backend stores quantity as an always-positive magnitude with
        // side tracked separately via direction ('BUY'/'LONG' vs
        // 'SELL'/'SHORT', see routes_terminal.py) — signed here the same
        // way the custom-strategy legs above already are, so a manually
        // shorted equity shows as negative qty and gets the isShort
        // (red/short) styling below instead of reading as a long position.
        qty: (pos.direction === "SELL" || pos.direction === "SHORT") ? -pos.quantity : pos.quantity,
        avg: pos.entry_price,
        ltp: ltps[pos.symbol] || pos.current_price || pos.entry_price,
        pnl: pos.unrealized_pnl ?? 0.00,
        pnlUnknown: pos.unrealized_pnl == null,
        chg: 0.00,
        isApi: true,
        isEquity: true,
      };
    });

  // Custom Strategy Builder legs merged in as ordinary rows — one unified
  // table rather than a separate section, since there's no natural
  // "Positions" concept that excludes them.
  const apiPositionsList: PositionListItem[] = [...legacyPositionsList, ...todayEquityPositions, ...customRows];

  const filteredPositions = apiPositionsList.filter(p => 
    p.symbol.toLowerCase().includes(searchQuery.toLowerCase())
  );

  // Calculate totals
  const totalPL = filteredPositions.reduce((sum, p) => sum + p.pnl, 0);

  // Determine max P&L for breakdown bar chart scaling
  const maxPnlAbs = Math.max(...filteredPositions.map(p => Math.abs(p.pnl)), 1);

  const handleClose = (item: PositionListItem) => {
    if (item.id) {
      if (item.isApi) {
        onClosePosition(item.id, item.isEquity ? "equity" : "options");
      } else {
        // Custom strategy positions have isApi: false
        onClosePosition(item.id, "custom");
      }
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
                      const rowKey = o.isApi ? (o.isEquity ? `equity-${o.id}` : `option-${o.id}`) : `custom-${o.id}`;
                      const isClosing = closingId === rowKey;
                      // Symbol is NOT a unique row identifier (two open
                      // positions can legitimately share an underlying,
                      // e.g. two strikes on the same index) — hovering one
                      // row previously revealed the Square Off button on
                      // every row sharing that symbol.
                      const isHovered = hoveredRowKey === rowKey;
                      const isShort = o.qty < 0;

                      return (
                        <tr
                          key={rowKey}
                          tabIndex={0}
                          onMouseEnter={() => setHoveredRowKey(rowKey)}
                          onMouseLeave={() => setHoveredRowKey(null)}
                          onFocus={() => setHoveredRowKey(rowKey)}
                          onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setHoveredRowKey(null); }}
                          className="hover:bg-gray-50 transition-colors focus:outline-none focus-visible:bg-gray-50"
                          style={{ height: "40px" }}
                        >
                          <td className="px-4 py-3 text-left w-12">
                            <input type="checkbox" className="rounded border-gray-300 text-orange-500 focus:ring-orange-500" />
                          </td>
                          <Td>
                            {o.product === "CNC" ? (
                              <span className="px-2 py-0.5 text-[10px] rounded font-bold bg-[#fff3e0] text-[#c17a3f]">
                                CNC
                              </span>
                            ) : o.product === "NRML" ? (
                              <span className="px-2 py-0.5 text-[10px] rounded font-bold bg-[#eef0fd] text-[#6b6fd1]">
                                NRML
                              </span>
                            ) : (
                              <span className="px-2 py-0.5 text-[10px] rounded font-bold bg-gray-100 text-gray-500">
                                {o.product}
                              </span>
                            )}
                          </Td>
                          <Td className="font-semibold text-gray-700">
                            <div className="flex items-baseline gap-1.5 text-[13px]">
                              <span>{o.displaySymbol ?? o.symbol}</span>
                              <span className="text-[9px] text-gray-400 uppercase font-semibold">{o.exch}</span>
                            </div>
                          </Td>
                          <Td right style={{ color: isShort ? C.red : C.blue }} className="font-semibold">
                            {o.qty}
                          </Td>
                          <Td right className="text-gray-700 font-mono">{inr(o.avg)}</Td>
                          <Td right className="text-gray-700 font-mono">{inr(o.ltp)}</Td>
                          <Td right style={{ color: o.pnlUnknown ? C.muted : sign(o.pnl) }} className="font-mono font-medium">
                            {o.pnlUnknown ? "—" : withSign(o.pnl)}
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
