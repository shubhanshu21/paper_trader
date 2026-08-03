import React, { useState } from "react";
import { useDispatch } from "react-redux";
import { useNavigate } from "react-router-dom";
import { User as UserType, LedgerItem, ChargeRates, api } from "../api";
import { Banner } from "./Common";
import { C, FONT, inr, fmtDate } from "../lib/format";
import { logout } from "../store/slices/authSlice";
import { AppDispatch } from "../store";

// Percentage-rate fields (everything except the flat per-order brokerage)
// are stored server-side as fractions (0.0015 = 0.15%) — edited here as
// human percentages and converted back to a fraction on save.
const PCT_FIELDS = ["exchange_charge_pct", "gst_pct", "stt_pct", "sebi_charge_pct", "stamp_duty_pct"] as const;

const CHARGE_FIELD_LABELS: Record<keyof ChargeRates, string> = {
  brokerage_per_order: "Brokerage (₹ per order)",
  exchange_charge_pct: "Exchange transaction charge (%)",
  gst_pct: "GST (%)",
  stt_pct: "STT — options sell side (%)",
  sebi_charge_pct: "SEBI charges (%)",
  stamp_duty_pct: "Stamp duty — buy side (%)",
};

function ratesToInputs(rates: ChargeRates): Record<keyof ChargeRates, string> {
  const out = {} as Record<keyof ChargeRates, string>;
  (Object.keys(rates) as (keyof ChargeRates)[]).forEach((k) => {
    const isPct = (PCT_FIELDS as readonly string[]).includes(k);
    out[k] = String(isPct ? rates[k] * 100 : rates[k]);
  });
  return out;
}

interface ProfileProps {
  currentUser: UserType | null;
  ledger: LedgerItem[];
  onRefreshData: () => void;
}

export default function ProfileView({ currentUser, ledger, onRefreshData }: ProfileProps) {
  const dispatch = useDispatch<AppDispatch>();
  const navigate = useNavigate();
  const [startingCapitalInput, setStartingCapitalInput] = useState("");
  const [adjustAmountInput, setAdjustAmountInput] = useState("");
  const [adjustNoteInput, setAdjustNoteInput] = useState("");
  const [feedback, setFeedback] = useState({ msg: "", isError: false });
  const [loading, setLoading] = useState(false);
  const [loggingOutAll, setLoggingOutAll] = useState(false);

  const [chargeRateInputs, setChargeRateInputs] = useState<Record<keyof ChargeRates, string> | null>(null);
  const [chargeRateDefaults, setChargeRateDefaults] = useState<ChargeRates | null>(null);
  const [chargeRatesBusy, setChargeRatesBusy] = useState(false);
  const [chargeRatesFeedback, setChargeRatesFeedback] = useState({ msg: "", isError: false });

  React.useEffect(() => {
    (async () => {
      try {
        const res = await api.getChargeRates();
        setChargeRateInputs(ratesToInputs(res.rates));
        setChargeRateDefaults(res.defaults);
      } catch {
        // Profile page still works without this card if the fetch fails — user can retry via Save.
      }
    })();
  }, []);

  const handleSaveChargeRates = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!chargeRateInputs) return;
    setChargeRatesFeedback({ msg: "", isError: false });
    setChargeRatesBusy(true);
    try {
      const payload: Partial<ChargeRates> = {};
      (Object.keys(chargeRateInputs) as (keyof ChargeRates)[]).forEach((k) => {
        const raw = parseFloat(chargeRateInputs[k]);
        if (!isNaN(raw)) {
          payload[k] = (PCT_FIELDS as readonly string[]).includes(k) ? raw / 100 : raw;
        }
      });
      const res = await api.setChargeRates(payload);
      setChargeRateInputs(ratesToInputs(res.rates));
      setChargeRatesFeedback({ msg: "Transaction cost rates updated.", isError: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : undefined;
      setChargeRatesFeedback({ msg: message || "Failed to update rates.", isError: true });
    } finally {
      setChargeRatesBusy(false);
    }
  };

  const handleResetChargeRates = async () => {
    setChargeRatesFeedback({ msg: "", isError: false });
    setChargeRatesBusy(true);
    try {
      const res = await api.setChargeRates({});
      setChargeRateInputs(ratesToInputs(res.rates));
      setChargeRatesFeedback({ msg: "Reset to the current default rates.", isError: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : undefined;
      setChargeRatesFeedback({ msg: message || "Failed to reset rates.", isError: true });
    } finally {
      setChargeRatesBusy(false);
    }
  };

  const [mfaEnabled, setMfaEnabled] = useState(!!currentUser?.mfa_enabled);
  const [mfaStep, setMfaStep] = useState<"idle" | "setup" | "backup-codes" | "disable">("idle");
  const [mfaSetupData, setMfaSetupData] = useState<{ secret: string; qrCodeDataUri: string } | null>(null);
  const [mfaConfirmCode, setMfaConfirmCode] = useState("");
  const [mfaBackupCodes, setMfaBackupCodes] = useState<string[]>([]);
  const [mfaDisablePassword, setMfaDisablePassword] = useState("");
  const [mfaDisableCode, setMfaDisableCode] = useState("");
  const [mfaError, setMfaError] = useState("");
  const [mfaBusy, setMfaBusy] = useState(false);

  // Keep local mfaEnabled in sync with the Redux-sourced currentUser prop
  // so changes from another tab (or a store update) are reflected immediately
  // without a page reload.
  React.useEffect(() => {
    setMfaEnabled(!!currentUser?.mfa_enabled);
  }, [currentUser?.mfa_enabled]);

  const handleStartMfaSetup = async () => {
    setMfaError("");
    setMfaBusy(true);
    try {
      const res = await api.mfaSetup();
      setMfaSetupData({ secret: res.secret, qrCodeDataUri: res.qr_code_data_uri });
      setMfaStep("setup");
    } catch (err) {
      const message = err instanceof Error ? err.message : undefined;
      setMfaError(message || "Could not start MFA setup.");
    } finally {
      setMfaBusy(false);
    }
  };

  const handleConfirmMfaSetup = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!mfaSetupData) return;
    setMfaError("");
    setMfaBusy(true);
    try {
      const res = await api.mfaConfirm(mfaSetupData.secret, mfaConfirmCode.trim());
      setMfaBackupCodes(res.backup_codes);
      setMfaStep("backup-codes");
      setMfaEnabled(true);
      setMfaConfirmCode("");
    } catch (err) {
      const message = err instanceof Error ? err.message : undefined;
      setMfaError(message || "Invalid code — check your authenticator app and try again.");
    } finally {
      setMfaBusy(false);
    }
  };

  const handleDisableMfa = async (e: React.FormEvent) => {
    e.preventDefault();
    setMfaError("");
    setMfaBusy(true);
    try {
      await api.mfaDisable(mfaDisablePassword, mfaDisableCode.trim());
      setMfaEnabled(false);
      setMfaStep("idle");
      setMfaDisablePassword("");
      setMfaDisableCode("");
    } catch (err) {
      const message = err instanceof Error ? err.message : undefined;
      setMfaError(message || "Could not disable MFA.");
    } finally {
      setMfaBusy(false);
    }
  };

  const handleDoneWithBackupCodes = () => {
    setMfaStep("idle");
    setMfaSetupData(null);
    setMfaBackupCodes([]);
  };

  const handleLogoutAll = async () => {
    setFeedback({ msg: "", isError: false });
    setLoggingOutAll(true);
    try {
      await api.logoutAll();
    } catch (err) {
      // Even if the request itself failed, the cookies are cleared client-side below —
      // worst case the server-side session isn't revoked, same as a network hiccup on regular logout.
      const message = err instanceof Error ? err.message : undefined;
      console.error("Logout-all request failed", message);
    } finally {
      setLoggingOutAll(false);
      dispatch(logout());
      navigate("/login");
    }
  };

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

  const boId = currentUser ? `1200000000${String(currentUser.id).padStart(6, '0')}` : "1200000000011111";
  const bankAcc = currentUser ? `XXXX-XXXX-${String(currentUser.id * 1234).slice(-4)} (ICICI BANK)` : "XXXX-XXXX-1234 (ICICI BANK)";

  const profileRows = [
    ["Email Address", currentUser?.email || "N/A"],
    ["User Role", currentUser?.role || "viewer"],
    ["Active Status", currentUser?.is_active ? "Active" : "Inactive"],
    ["Demat BO ID", boId],
    ["Bank Account", bankAcc],
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
          <h3 className="text-lg font-medium text-gray-800 mb-4">
            <span>Demat Account Info</span>
          </h3>
          <div className="space-y-3">
            {profileRows.map(([label, val], idx) => (
              <div key={idx} className="flex justify-between border-b pb-2 text-sm text-gray-700">
                <span className="text-gray-400">{label}</span>
                <span className="font-semibold">{val}</span>
              </div>
            ))}
          </div>

          <div className="pt-4 mt-4 border-t flex justify-between items-center">
            <div>
              <span className="text-xs text-gray-700 font-semibold block">Security</span>
              <span className="text-[11px] text-gray-400">Sign out of this session on every device/browser it's currently logged in on.</span>
            </div>
            <button
              type="button"
              onClick={handleLogoutAll}
              disabled={loggingOutAll}
              className="px-4 py-2 text-xs font-semibold text-white rounded hover:opacity-90 disabled:opacity-50 shadow-sm shrink-0 ml-3"
              style={{ backgroundColor: C.orange }}
            >
              {loggingOutAll ? "Logging out..." : "Log Out Everywhere"}
            </button>
          </div>

          {/* Two-Factor Authentication */}
          <div className="pt-4 mt-4 border-t">
            <div className="flex justify-between items-center">
              <div>
                <span className="text-xs text-gray-700 font-semibold block">Two-Factor Authentication</span>
                <span className="text-[11px] text-gray-400">
                  {mfaEnabled ? "Enabled — an authenticator code is required at login." : "Add an authenticator app code as a second login step."}
                </span>
              </div>
              {mfaStep === "idle" && (
                mfaEnabled ? (
                  <button
                    type="button"
                    onClick={() => { setMfaStep("disable"); setMfaError(""); }}
                    className="px-4 py-2 text-xs font-semibold text-red-600 border border-red-200 rounded hover:bg-red-50 shrink-0 ml-3"
                  >
                    Disable
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={handleStartMfaSetup}
                    disabled={mfaBusy}
                    className="px-4 py-2 text-xs font-semibold text-white rounded hover:opacity-90 disabled:opacity-50 shadow-sm shrink-0 ml-3"
                    style={{ backgroundColor: C.blue }}
                  >
                    {mfaBusy ? "Starting..." : "Enable"}
                  </button>
                )
              )}
            </div>

            {mfaError && mfaStep !== "idle" && (
              <div className="mt-3 px-3 py-2 rounded text-xs border bg-red-50 border-red-200 text-red-600">{mfaError}</div>
            )}

            {mfaStep === "setup" && mfaSetupData && (
              <div className="mt-4 p-4 rounded border bg-gray-50 space-y-3" style={{ borderColor: C.border2 }}>
                <p className="text-xs text-gray-600">Scan this QR code with Google Authenticator, Authy, or any TOTP app — or enter the key manually.</p>
                <img src={mfaSetupData.qrCodeDataUri} alt="MFA QR code" className="w-40 h-40 border rounded bg-white p-1" style={{ borderColor: C.border2 }} />
                <div className="text-[11px] font-mono bg-white border rounded px-2 py-1.5 break-all" style={{ borderColor: C.border2 }}>
                  {mfaSetupData.secret}
                </div>
                <form onSubmit={handleConfirmMfaSetup} className="flex gap-2 pt-1">
                  <input
                    type="text"
                    required
                    autoFocus
                    inputMode="numeric"
                    placeholder="6-digit code"
                    value={mfaConfirmCode}
                    onChange={(e) => setMfaConfirmCode(e.target.value)}
                    className="flex-1 px-3 py-2 border rounded focus:ring-1 focus:ring-orange-500 outline-none text-sm font-mono tracking-widest"
                  />
                  <button
                    type="submit"
                    disabled={mfaBusy}
                    className="px-4 py-2 text-xs font-semibold text-white bg-blue-500 rounded hover:bg-blue-600 disabled:bg-gray-300 shadow-sm"
                  >
                    Confirm
                  </button>
                  <button
                    type="button"
                    onClick={() => { setMfaStep("idle"); setMfaSetupData(null); setMfaConfirmCode(""); setMfaError(""); }}
                    className="px-3 py-2 text-xs font-semibold text-gray-500 hover:text-gray-700"
                  >
                    Cancel
                  </button>
                </form>
              </div>
            )}

            {mfaStep === "backup-codes" && (
              <div className="mt-4 p-4 rounded border bg-amber-50 space-y-3" style={{ borderColor: "#fde68a" }}>
                <p className="text-xs font-semibold text-amber-800">Save these backup codes now — each works once, and this is the only time they're shown.</p>
                <div className="grid grid-cols-2 gap-2 font-mono text-xs bg-white border rounded p-3" style={{ borderColor: C.border2 }}>
                  {mfaBackupCodes.map((code) => <div key={code}>{code}</div>)}
                </div>
                <button
                  type="button"
                  onClick={handleDoneWithBackupCodes}
                  className="px-4 py-2 text-xs font-semibold text-white bg-blue-500 rounded hover:bg-blue-600 shadow-sm"
                >
                  I've saved these codes
                </button>
              </div>
            )}

            {mfaStep === "disable" && (
              <form onSubmit={handleDisableMfa} className="mt-4 p-4 rounded border bg-gray-50 space-y-3" style={{ borderColor: C.border2 }}>
                <p className="text-xs text-gray-600">Confirm your password and a current authenticator (or backup) code to disable two-factor authentication.</p>
                <input
                  type="password"
                  required
                  placeholder="Current password"
                  value={mfaDisablePassword}
                  onChange={(e) => setMfaDisablePassword(e.target.value)}
                  className="w-full px-3 py-2 border rounded focus:ring-1 focus:ring-orange-500 outline-none text-sm"
                />
                <input
                  type="text"
                  required
                  inputMode="numeric"
                  placeholder="Authenticator or backup code"
                  value={mfaDisableCode}
                  onChange={(e) => setMfaDisableCode(e.target.value)}
                  className="w-full px-3 py-2 border rounded focus:ring-1 focus:ring-orange-500 outline-none text-sm font-mono"
                />
                <div className="flex gap-2">
                  <button
                    type="submit"
                    disabled={mfaBusy}
                    className="px-4 py-2 text-xs font-semibold text-white bg-red-600 rounded hover:bg-red-700 disabled:opacity-50 shadow-sm"
                  >
                    {mfaBusy ? "Disabling..." : "Disable MFA"}
                  </button>
                  <button
                    type="button"
                    onClick={() => { setMfaStep("idle"); setMfaDisablePassword(""); setMfaDisableCode(""); setMfaError(""); }}
                    className="px-3 py-2 text-xs font-semibold text-gray-500 hover:text-gray-700"
                  >
                    Cancel
                  </button>
                </div>
              </form>
            )}
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

      {/* Transaction Cost Rates (F&O) */}
      <div className="bg-white p-6 border rounded-lg shadow-sm mb-12">
        <div className="flex items-start justify-between mb-1">
          <h3 className="text-lg font-medium text-gray-800">Transaction Cost Rates (F&O)</h3>
          <button
            type="button"
            onClick={handleResetChargeRates}
            disabled={chargeRatesBusy || !chargeRateInputs}
            className="px-3 py-1.5 text-xs font-semibold text-gray-600 border rounded hover:bg-gray-50 disabled:opacity-50 shrink-0 ml-3"
            style={{ borderColor: C.border2 }}
          >
            Reset to Defaults
          </button>
        </div>
        <p className="text-xs text-gray-500 mb-4">
          Every backtest/paper/live P&L figure is computed net of these Indian F&O charges (brokerage, exchange
          transaction charge, GST, STT, SEBI charges, stamp duty). NSE/SEBI/the government revise these rates from
          time to time — update them here to keep P&L accurate without waiting on an app update.
        </p>

        {chargeRatesFeedback.msg && (
          <div className={`px-4 py-2 mb-4 rounded text-xs border ${chargeRatesFeedback.isError ? "bg-red-50 border-red-200 text-red-600" : "bg-green-50 border-green-200 text-green-600"}`}>
            {chargeRatesFeedback.msg}
          </div>
        )}

        {!chargeRateInputs ? (
          <div className="text-xs text-gray-400">Loading current rates...</div>
        ) : (
          <form onSubmit={handleSaveChargeRates} className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {(Object.keys(CHARGE_FIELD_LABELS) as (keyof ChargeRates)[]).map((field) => (
                <div key={field}>
                  <label className="block text-[11px] font-medium text-gray-600 mb-1">{CHARGE_FIELD_LABELS[field]}</label>
                  <div className="relative">
                    <input
                      type="number"
                      step="any"
                      value={chargeRateInputs[field]}
                      onChange={(e) => setChargeRateInputs((prev) => prev && { ...prev, [field]: e.target.value })}
                      className="w-full px-3 py-2 border rounded focus:ring-1 focus:ring-orange-500 outline-none text-sm"
                      style={{ borderColor: C.border2 }}
                    />
                  </div>
                  {chargeRateDefaults && (
                    <p className="text-[10px] text-gray-400 mt-1">
                      Default: {(PCT_FIELDS as readonly string[]).includes(field) ? (chargeRateDefaults[field] * 100) : chargeRateDefaults[field]}
                      {(PCT_FIELDS as readonly string[]).includes(field) ? "%" : ""}
                    </p>
                  )}
                </div>
              ))}
            </div>
            <button
              type="submit"
              disabled={chargeRatesBusy}
              className="px-4 py-2 text-xs font-semibold text-white bg-blue-500 rounded hover:bg-blue-600 disabled:bg-gray-300 shadow-sm"
            >
              {chargeRatesBusy ? "Saving..." : "Save Rates"}
            </button>
          </form>
        )}
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
