import { useState } from "react";
import { Search, AlertTriangle } from "lucide-react";
import { C, FONT } from "../lib/format";

interface IpoItem {
  symbol: string;
  name: string;
  startDate: string;
  endDate: string;
  priceRange: string;
  minAmount: number;
  qty: number;
  status: "APPLY" | "CLOSED";
}

const IPO_DATA: IpoItem[] = [
  {
    symbol: "NXST",
    name: "Nexus Select Trust",
    startDate: "9th",
    endDate: "11th May 2023",
    priceRange: "95 - 100",
    minAmount: 14250,
    qty: 150,
    status: "APPLY",
  },
  {
    symbol: "INNOKAIZ",
    name: "Innokaiz India Limited",
    startDate: "28th Apr",
    endDate: "3rd May 2023",
    priceRange: "76 - 78",
    minAmount: 121600,
    qty: 1600,
    status: "CLOSED",
  },
  {
    symbol: "DENEERS",
    name: "De Neers Tools Limited",
    startDate: "28th Apr",
    endDate: "3rd May 2023",
    priceRange: "95 - 101",
    minAmount: 114000,
    qty: 1200,
    status: "CLOSED",
  },
  {
    symbol: "MANKIND",
    name: "Mankind Pharma Limited",
    startDate: "25th",
    endDate: "27th Apr 2023",
    priceRange: "1026 - 1080",
    minAmount: 13338,
    qty: 13,
    status: "CLOSED",
  },
  {
    symbol: "RETINA",
    name: "Retina Paints Limited",
    startDate: "19th",
    endDate: "24th Apr 2023",
    priceRange: "30 - 30",
    minAmount: 120000,
    qty: 4000,
    status: "CLOSED",
  },
];

export default function BidsView() {
  const [subTab, setSubTab] = useState<"Auctions" | "IPO" | "Govt. securities">("IPO");
  const [searchQuery, setSearchQuery] = useState("");
  const [applied, setApplied] = useState<{ [key: string]: boolean }>({});

  const filteredIpos = IPO_DATA.filter((ipo) =>
    ipo.symbol.toLowerCase().includes(searchQuery.toLowerCase()) ||
    ipo.name.toLowerCase().includes(searchQuery.toLowerCase())
  );

  const handleApply = (symbol: string) => {
    setApplied((prev) => ({ ...prev, [symbol]: true }));
  };

  return (
    <div className="w-full" style={FONT}>
      {/* Sub tabs */}
      <div className="flex gap-8 border-b text-xs pb-3 mb-6" style={{ borderColor: C.border2 }}>
        {(["Auctions", "IPO", "Govt. securities"] as const).map((tab) => {
          const active = subTab === tab;
          return (
            <button
              key={tab}
              onClick={() => setSubTab(tab)}
              className="font-semibold transition-colors focus:outline-none relative"
              style={{ color: active ? C.orange : C.muted }}
            >
              {tab}
              {active && (
                <span
                  className="absolute bottom-[-13px] left-0 right-0 h-[2px]"
                  style={{ background: C.orange }}
                />
              )}
            </button>
          );
        })}
      </div>

      {subTab !== "IPO" ? (
        <div className="py-20 text-sm text-center border-dashed border-2 rounded-lg bg-gray-50 text-gray-400 font-medium">
          No active {subTab.toLowerCase()} bids open for application.
        </div>
      ) : (
        <div className="space-y-6">
          {/* Banner */}
          <div
            className="flex items-center gap-2 px-4 py-3 text-sm rounded border"
            style={{ background: C.banner, borderColor: C.bannerBorder, color: C.text }}
          >
            <AlertTriangle size={15} style={{ color: "#e6a532" }} />
            <span>
              This is a demo platform with dummy data.{" "}
              <a href="#" style={{ color: C.blue }} className="font-semibold hover:underline">
                Signup now
              </a>{" "}
              to access the live platform.
            </span>
          </div>

          {/* Heading and Search bar */}
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold text-gray-700">IPOs ({filteredIpos.length})</h2>

            <div
              className="flex items-center gap-2 px-3 py-1.5 rounded bg-white border text-xs"
              style={{ borderColor: C.border2 }}
            >
              <Search size={13} style={{ color: C.faint }} />
              <input
                placeholder="Search"
                className="outline-none w-28 bg-transparent text-gray-700"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
              />
            </div>
          </div>

          {/* IPO List Table */}
          <div className="bg-white border rounded shadow-xs overflow-hidden" style={{ borderColor: C.tableBorder }}>
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b text-xs font-semibold uppercase tracking-wider" style={{ borderColor: C.border2, background: C.tableHeaderBg, color: C.tableHeaderText }}>
                  <th className="px-6 py-3.5">Instrument</th>
                  <th className="px-6 py-3.5">Date</th>
                  <th className="px-6 py-3.5 text-right">Price range (₹)</th>
                  <th className="px-6 py-3.5 text-right">Min. amount (₹)</th>
                  <th className="px-6 py-3.5 text-right"></th>
                </tr>
              </thead>
              <tbody className="divide-y text-sm" style={{ borderColor: C.border }}>
                {filteredIpos.map((ipo) => (
                  <tr key={ipo.symbol} className="hover:bg-gray-50 transition-colors" style={{ height: "40px" }}>
                    {/* Instrument */}
                    <td className="px-6 py-4">
                      <div className="flex flex-col">
                        <span className="font-bold text-gray-800">{ipo.symbol}</span>
                        <span className="text-xs text-gray-400 font-medium">{ipo.name}</span>
                      </div>
                    </td>

                    {/* Date */}
                    <td className="px-6 py-4 text-gray-600 font-medium">
                      {ipo.startDate} — {ipo.endDate}
                    </td>

                    {/* Price Range */}
                    <td className="px-6 py-4 text-right text-gray-800 font-mono font-medium">
                      {ipo.priceRange}
                    </td>

                    {/* Min. Amount & Qty */}
                    <td className="px-6 py-4 text-right">
                      <div className="flex flex-col">
                        <span className="font-mono font-bold text-gray-800">{ipo.minAmount.toLocaleString("en-IN")}</span>
                        <span className="text-[11px] text-gray-400 font-medium">{ipo.qty} Qty.</span>
                      </div>
                    </td>

                    {/* Action button */}
                    <td className="px-6 py-4 text-right">
                      {ipo.status === "APPLY" ? (
                        applied[ipo.symbol] ? (
                          <span className="inline-block px-4 py-1.5 text-xs font-semibold rounded bg-green-50 text-green-600 border border-green-200">
                            Applied
                          </span>
                        ) : (
                          <button
                            onClick={() => handleApply(ipo.symbol)}
                            className="px-6 py-1.5 text-xs font-bold text-white rounded shadow-sm hover:opacity-90 transition-all focus:outline-none"
                            style={{ background: "#2196f3" }}
                          >
                            Apply
                          </button>
                        )
                      ) : (
                        <span className="inline-block px-4 py-1.5 text-xs font-semibold rounded bg-gray-100 text-gray-400 font-bold">
                          CLOSED
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Link to upcoming */}
          <div className="text-center py-2">
            <a href="#" style={{ color: C.blue }} className="text-xs font-medium hover:underline">
              Don't see an IPO here? View upcoming →
            </a>
          </div>

          {/* Bottom Alerts (exact layout from screenshot) */}
          <div className="pt-6 border-t" style={{ borderColor: C.border2 }}>
            <div className="flex items-start gap-2.5 text-xs text-gray-500">
              <AlertTriangle size={15} style={{ color: "#d9534f" }} className="mt-0.5" />
              <div className="flex flex-col">
                <span className="font-bold text-gray-700">Applications couldn't be loaded.</span>
                <span className="text-gray-400 mt-0.5 font-mono">Not found (GeneralException)</span>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
