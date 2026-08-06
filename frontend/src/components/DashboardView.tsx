import { PieChart, Briefcase, Layers, TrendingUp, FileText } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { WalletSummary, EquityPosition, OptionsPosition, User } from "../api";
import { Banner, SectionTitle, ColorBar } from "./Common";
import { C, FONT, withSign, sign, inrWithSignNoPlus } from "../lib/format";
import { useCustomStrategyPositions } from "../hooks/useCustomStrategyPositions";

interface DashboardProps {
  currentUser?: User | null;
  wallet: WalletSummary | null;
  openEquity: EquityPosition[];
  openOptions: OptionsPosition[];
  setPage: (page: string) => void;
  walletLoading?: boolean;
}

// A minimal shape both legacy options positions and custom-strategy-
// builder legs can be flattened into for the summary panel below —
// PositionsView.tsx merges these two sources the same way (plus equity),
// this only needs id/symbol/quantity/pnl for its compact list.
interface SummaryRow {
  key: string;
  symbol: string;
  quantity: number;
  pnl: number;
}

export default function DashboardView({ currentUser, wallet, openEquity, openOptions, walletLoading }: DashboardProps) {
  const navigate = useNavigate();
  const customRows = useCustomStrategyPositions();
  const holdingsCount = openEquity.length;
  // Previously only openOptions — a user with ONLY custom-strategy-builder
  // legs open (no legacy strangle positions) saw "0 active positions" here
  // even though the Positions page correctly showed their open legs.
  const summaryRows: SummaryRow[] = [
    ...openOptions.map((pos) => ({ key: `opt-${pos.id}`, symbol: pos.symbol, quantity: pos.quantity, pnl: pos.mtm || 0 })),
    ...customRows.map((r) => ({ key: `custom-${r.id}`, symbol: r.symbol, quantity: r.quantity, pnl: r.pnl || 0 })),
  ];
  const positionsCount = summaryRows.length;
  const displayName = currentUser?.username || "Trader";

  return (
     <div className="w-full" style={FONT}>
      <Banner />
      <h1 className="mb-6 text-3xl font-light text-gray-800">
        Hi, {displayName}
      </h1>
      <div style={{ borderTop: `1px solid ${C.border}` }} className="pt-8" />

      {/* Balances */}
      <div className="grid gap-12 mb-10 md:grid-cols-2">
        <div className="flex gap-8 bg-white p-5 border rounded-lg shadow-sm">
          <div className="flex-1">
            <SectionTitle icon={PieChart}>Equity Margin</SectionTitle>
            {walletLoading ? (
              <div className="h-10 w-40 rounded animate-pulse" style={{ backgroundColor: C.border }} />
            ) : (
              <div className="text-4xl font-light text-gray-800">
                {inrWithSignNoPlus(wallet?.available_balance)}
              </div>
            )}
            <div className="mt-2 text-xs text-gray-400 font-medium">
              Margin available
            </div>
          </div>
          <div className="flex-1 pt-6 space-y-3 pl-6 border-l" style={{ borderColor: C.border }}>
            <div className="flex justify-between text-sm">
              <span className="text-gray-400">Margins used</span>
              <span className="font-semibold text-gray-700">{inrWithSignNoPlus(wallet?.margin_blocked)}</span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-400">Opening balance</span>
              <span className="font-semibold text-gray-700">{inrWithSignNoPlus(wallet?.starting_capital)}</span>
            </div>
          </div>
        </div>
      </div>

      <div style={{ borderTop: `1px solid ${C.border}` }} className="pt-8" />

      {/* Holdings & Positions Summaries */}
      <div className="grid gap-8 mb-10 md:grid-cols-3">
        <div className="md:col-span-2 bg-white p-6 border rounded-lg shadow-sm">
          <SectionTitle icon={Briefcase}>Holdings Summary ({holdingsCount})</SectionTitle>
          <div className="flex gap-10">
            <div className="flex-1">
              <div className="flex items-baseline gap-3">
                <span className="text-4xl font-light" style={{ color: wallet && wallet.realized_pnl_lifetime >= 0 ? C.green : C.red }}>
                  {inrWithSignNoPlus(wallet?.realized_pnl_lifetime)}
                </span>
                <span className="text-xs font-semibold" style={{ color: wallet && wallet.realized_pnl_lifetime >= 0 ? C.green : C.red }}>
                  {wallet && wallet.starting_capital > 0 ? `${withSign((wallet.realized_pnl_lifetime / wallet.starting_capital) * 100)}%` : "0.00%"}
                </span>
              </div>
              <div className="mt-2 text-xs text-gray-400 font-medium">
                Realized P&amp;L (Lifetime)
              </div>
            </div>
            <div className="flex-1 pt-2 space-y-3 pl-8 border-l" style={{ borderColor: C.border }}>
              <div className="flex justify-between text-sm">
                <span className="text-gray-400">Total Charges</span>
                <span className="font-medium text-gray-700">{inrWithSignNoPlus(wallet?.total_charges_lifetime)}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-400">Adjustments</span>
                <span className="font-medium text-gray-700">{inrWithSignNoPlus(wallet?.total_adjustments)}</span>
              </div>
            </div>
          </div>
          <div className="mt-6">
            <ColorBar height={24} />
            <div className="flex items-center justify-between mt-3 text-sm">
              <span className="font-semibold text-gray-700">{inrWithSignNoPlus(wallet?.available_balance)} available</span>
              <span className="text-xs text-gray-400 font-medium">Derived from paper transactions</span>
            </div>
          </div>
        </div>

        <div className="p-6 rounded-lg bg-slate-50 border flex flex-col justify-between shadow-sm">
          <div className="text-xl font-medium leading-snug text-gray-800">
            Paper Trading Dashboard
            <div className="text-xs font-normal text-gray-400 mt-1">
              Add custom balance or reset history from the Profile menu.
            </div>
          </div>
          <div className="flex items-end justify-between mt-6">
            <div className="w-24 h-12 rounded bg-amber-500 opacity-20" />
            <Layers size={44} style={{ color: C.blue }} />
          </div>
        </div>
      </div>

      <div style={{ borderTop: `1px solid ${C.border}` }} className="pt-8" />

      {/* Market Overview + Positions */}
      <div className="grid gap-12 md:grid-cols-2">
        <div>
          <SectionTitle icon={TrendingUp}>Market Overview</SectionTitle>
          <div className="flex items-center gap-2 mb-2 text-xs font-medium text-gray-500">
            <span className="inline-block w-2 h-2 rounded-full" style={{ background: C.blue }} /> NIFTY 50 Index
            <span className="ml-auto text-[10px] text-gray-400 italic">Illustrative — live chart coming soon</span>
          </div>
          <svg viewBox="0 0 348 100" className="w-full bg-slate-50 p-2 rounded border animate-fade-in" style={{ height: 150 }}>
            {[20, 50, 80].map((y) => (
              <line key={y} x1="0" y1={y} x2="348" y2={y} stroke={C.border} strokeWidth="1" />
            ))}
            <polyline
              points="0,72 22,45 40,58 58,88 76,74 96,40 114,30 132,44 150,36 168,14 186,6 204,18 220,10 236,26 252,20 268,32 286,60 300,44 316,52 332,22 348,8"
              fill="none"
              stroke={C.blue}
              strokeWidth="1.6"
            />
          </svg>
          <div className="flex justify-between px-2 mt-1 text-[10px] text-gray-400 font-medium">
            <span>Jul 22</span>
            <span>Oct 22</span>
            <span>Jan 23</span>
            <span>Apr 23</span>
          </div>
        </div>

        <div>
          <SectionTitle icon={FileText}>Active Positions Summary ({positionsCount})</SectionTitle>
          {summaryRows.length === 0 ? (
            <div className="py-8 text-center text-sm text-gray-400 border border-dashed rounded-lg bg-gray-50">
              No active positions.
            </div>
          ) : (
            <div className="space-y-3 max-h-48 overflow-y-auto pr-2">
              {summaryRows.slice(0, 5).map((row) => (
                <div key={row.key} className="flex justify-between items-center text-sm border-b pb-2">
                  <div className="flex flex-col">
                    <span className="font-semibold text-gray-800">{row.symbol}</span>
                    <span className="text-[10px] text-gray-400">Qty: {row.quantity}</span>
                  </div>
                  <span className="font-semibold tabular-nums" style={{ color: sign(row.pnl) }}>
                    {withSign(row.pnl)}
                  </span>
                </div>
              ))}
              {summaryRows.length > 5 && (
                <button onClick={() => navigate("/positions")} className="text-xs text-blue-500 hover:underline font-semibold block text-center w-full py-1 focus:outline-none">
                  View all {summaryRows.length} positions
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
