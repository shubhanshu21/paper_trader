import React, { useState } from "react";
import { User as UserType, LedgerItem, api } from "../api";
import { C, FONT, inr, Banner, fmtDate } from "./Common";

interface ProfileProps {
  currentUser: UserType | null;
  ledger: LedgerItem[];
  onRefreshData: () => void;
}

export default function ProfileView({ currentUser, ledger, onRefreshData }: ProfileProps) {
  const [startingCapitalInput, setStartingCapitalInput] = useState("");
  const [adjustAmountInput, setAdjustAmountInput] = useState("");
  const [adjustNoteInput, setAdjustNoteInput] = useState("");
  const [feedback, setFeedback] = useState({ msg: "", isError: false });
  const [loading, setLoading] = useState(false);

  const handleSetCapital = async (e: React.FormEvent) => {
    e.preventDefault();
    setFeedback({ msg: "", isError: false });
    const val = parseFloat(startingCapitalInput);
    if (isNaN(val) || val < 0) {
      setFeedback({ msg: "Please enter a valid starting capital amount.", isError: true });
      return;
    }
    setLoading(true);
    try {
      await api.setStartingCapital(val);
      setFeedback({ msg: `Starting capital updated to ₹${inr(val)} successfully! (Wiped paper-trading history)`, isError: false });
      setStartingCapitalInput("");
      onRefreshData();
    } catch (err) {
      const message = err instanceof Error ? err.message : undefined;
      setFeedback({ msg: message || "Failed to update capital.", isError: true });
    } finally {
      setLoading(false);
    }
  };

  const handleAdjustWallet = async (e: React.FormEvent) => {
    e.preventDefault();
    setFeedback({ msg: "", isError: false });
    const val = parseFloat(adjustAmountInput);
    if (isNaN(val)) {
      setFeedback({ msg: "Please enter a valid numerical amount.", isError: true });
      return;
    }
    setLoading(true);
    try {
      await api.adjustWallet(val, adjustNoteInput || "Manual Adjust");
      setFeedback({ msg: `Wallet adjusted by ₹${inr(val)} successfully!`, isError: false });
      setAdjustAmountInput("");
      setAdjustNoteInput("");
      onRefreshData();
    } catch (err) {
      const message = err instanceof Error ? err.message : undefined;
      setFeedback({ msg: message || "Adjustment failed.", isError: true });
    } finally {
      setLoading(false);
    }
  };

  const handleResetWallet = async () => {
    setFeedback({ msg: "", isError: false });
    setLoading(true);
    try {
      await api.resetWallet();
      setFeedback({ msg: "Database records reset successfully. All paper trading history cleared.", isError: false });
      onRefreshData();
    } catch (err) {
      const message = err instanceof Error ? err.message : undefined;
      setFeedback({ msg: message || "Reset failed.", isError: true });
    } finally {
      setLoading(false);
    }
  };

  const profileRows = [
    ["Email Address", currentUser?.email || "N/A"],
    ["User Role", currentUser?.role || "viewer"],
    ["Active Status", currentUser?.is_active ? "Active" : "Inactive"],
    ["Demat BO ID", "1200000000011111"],
    ["Bank Account", "XXXX-XXXX-1234 (ICICI BANK)"],
  ];

  return (
    <div className="w-full" style={FONT}>
      <Banner />
      
      {feedback.msg && (
        <div className={`px-4 py-3 mb-6 rounded text-sm border ${feedback.isError ? "bg-red-50 border-red-200 text-red-600" : "bg-green-50 border-green-200 text-green-600"}`}>
          {feedback.msg}
        </div>
      )}

      <div className="flex items-center gap-6 pb-6 border-b">
        <div className="w-16 h-16 rounded-full bg-blue-500 text-white flex items-center justify-center text-3xl font-light shadow">
          {currentUser ? currentUser.username[0].toUpperCase() : "U"}
        </div>
        <div>
          <h2 className="text-2xl font-semibold text-gray-800">
            {currentUser?.username || "Guest User"}
          </h2>
          <span className="text-xs text-gray-400 font-semibold font-mono uppercase tracking-wider">{currentUser?.role || "viewer"} account</span>
        </div>
      </div>

      <div className="grid gap-12 py-8 md:grid-cols-2">
        {/* Account Details */}
        <div className="bg-white p-6 border rounded-lg shadow-sm">
          <h3 className="text-lg font-medium text-gray-800 mb-4 flex justify-between items-center">
            <span>Demat Account Info</span>
            <span className="text-xs text-blue-500 font-semibold cursor-pointer hover:underline">Manage Settings</span>
          </h3>
          <div className="space-y-3">
            {profileRows.map(([label, val], idx) => (
              <div key={idx} className="flex justify-between border-b pb-2 text-sm text-gray-700">
                <span className="text-gray-400">{label}</span>
                <span className="font-semibold">{val}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Paper Balance / Capital Configurations */}
        <div className="bg-white p-6 border rounded-lg shadow-sm space-y-6">
          {/* Form: Set Capital */}
          <form onSubmit={handleSetCapital} className="space-y-3">
            <h3 className="text-sm font-semibold text-gray-700">Set Virtual Starting Capital</h3>
            <div className="flex gap-2">
              <input
                type="number"
                required
                className="flex-1 px-3 py-2 border rounded focus:ring-1 focus:ring-orange-500 outline-none text-sm"
                placeholder="e.g. 100000"
                value={startingCapitalInput}
                onChange={(e) => setStartingCapitalInput(e.target.value)}
              />
              <button
                type="submit"
                disabled={loading}
                className="px-4 py-2 text-xs font-semibold text-white bg-blue-500 rounded hover:bg-blue-600 disabled:bg-gray-300 shadow-sm"
              >
                Set Capital
              </button>
            </div>
          </form>

          {/* Form: Adjust Wallet */}
          <form onSubmit={handleAdjustWallet} className="space-y-3 pt-3 border-t">
            <h3 className="text-sm font-semibold text-gray-700">Add / Withdraw Balance Adjustments</h3>
            <div className="flex gap-2">
              <input
                type="number"
                required
                className="w-1/3 px-3 py-2 border rounded focus:ring-1 focus:ring-orange-500 outline-none text-sm"
                placeholder="e.g. 5000 or -2000"
                value={adjustAmountInput}
                onChange={(e) => setAdjustAmountInput(e.target.value)}
              />
              <input
                type="text"
                className="flex-1 px-3 py-2 border rounded focus:ring-1 focus:ring-orange-500 outline-none text-sm"
                placeholder="Adjustment description/note"
                value={adjustNoteInput}
                onChange={(e) => setAdjustNoteInput(e.target.value)}
              />
              <button
                type="submit"
                disabled={loading}
                className="px-4 py-2 text-xs font-semibold text-white bg-blue-500 rounded hover:bg-blue-600 disabled:bg-gray-300 shadow-sm"
              >
                Apply
              </button>
            </div>
          </form>

          {/* Button: Reset Database */}
          <div className="pt-4 border-t flex justify-between items-center">
            <span className="text-xs text-red-500 font-semibold font-medium">Danger Zone</span>
            <button
              type="button"
              onClick={handleResetWallet}
              disabled={loading}
              className="px-4 py-2 text-xs font-semibold text-white bg-red-600 rounded hover:bg-red-700 disabled:bg-gray-300 shadow-sm"
            >
              Reset All Trade History
            </button>
          </div>
        </div>
      </div>

      {/* Statement */}
      <div className="border-t pt-8 pb-4" style={{ borderColor: C.border2 }}>
        <h3 className="text-[15px] font-semibold text-gray-700 mb-4">Statement ({ledger.length})</h3>
        {ledger.length === 0 ? (
          <div className="py-12 text-center text-xs text-gray-400 border border-dashed rounded bg-gray-50">
            No ledger entries found. Perform virtual trade executions to populate the statement registry.
          </div>
        ) : (
          <div className="overflow-x-auto bg-white rounded border shadow-xs" style={{ borderColor: C.tableBorder }}>
            <table className="w-full border-collapse">
              <thead>
                <tr className="border-b text-xs" style={{ borderColor: C.border2, background: C.tableHeaderBg }}>
                  <th className="px-4 py-3 text-left font-medium">Date</th>
                  <th className="px-4 py-3 text-left font-medium">Description</th>
                  <th className="px-4 py-3 text-right font-medium">Debit</th>
                  <th className="px-4 py-3 text-right font-medium">Credit</th>
                  <th className="px-4 py-3 text-right font-medium">Running Balance</th>
                </tr>
              </thead>
              <tbody className="divide-y text-xs" style={{ borderColor: C.border }}>
                {ledger.map((item, idx) => (
                  <tr key={idx} className="hover:bg-gray-50 transition-colors" style={{ height: "40px" }}>
                    <td className="px-4 py-3 text-gray-500 font-medium font-mono">{fmtDate(item.date)}</td>
                    <td className="px-4 py-3 text-gray-800 font-medium">{item.description}</td>
                    <td className="px-4 py-3 text-right text-red-600 font-normal tabular-nums">
                      {item.debit > 0 ? `-${inr(item.debit)}` : "—"}
                    </td>
                    <td className="px-4 py-3 text-right text-green-600 font-normal tabular-nums">
                      {item.credit > 0 ? `+${inr(item.credit)}` : "—"}
                    </td>
                    <td className="px-4 py-3 text-right font-medium text-gray-700 tabular-nums">
                      {inr(item.balance)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
