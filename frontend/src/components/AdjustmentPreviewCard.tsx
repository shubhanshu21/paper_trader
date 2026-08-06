import { useState } from "react";
import { Scale, RefreshCw, AlertTriangle } from "lucide-react";
import { api, ApiError, AdjustmentPreviewResponse } from "../api";
import { C, inr, withSign, sign } from "../lib/format";

/**
 * On-demand "what would the automatic adjustment do right now" preview
 * for SMART_CONDOR / DELTA_NEUTRAL_STRANGLE strategies (the two engines
 * with a live-premium/delta-triggered mid-cycle adjustment) — see
 * api/routes_adjustment_preview.py, which mirrors each engine's own tick
 * logic read-only (no orders placed, nothing written) so this always
 * reflects exactly what the next real tick would do. Fetched on click,
 * not polled — each call is a real Upstox Margin Calculator basket call
 * plus a live chain/premium read, not something to hit on a timer.
 */
export default function AdjustmentPreviewCard({ strategyId }: { strategyId: number }) {
  const [preview, setPreview] = useState<AdjustmentPreviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchPreview = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getAdjustmentPreview(strategyId);
      setPreview(data);
    } catch (err) {
      const detail = err instanceof ApiError && err.detail ? (Array.isArray(err.detail) ? err.detail.join(" ") : err.detail) : null;
      setError(detail || (err instanceof Error ? err.message : "Could not load the adjustment preview."));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="rounded-xl border p-4" style={{ borderColor: C.border2 }}>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <Scale size={14} style={{ color: C.muted }} />
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Adjustment Preview</h3>
        </div>
        <button
          onClick={fetchPreview}
          disabled={loading}
          className="flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-semibold rounded-md hover:bg-gray-100 disabled:opacity-50 focus:outline-none"
          style={{ color: C.orange }}
        >
          <RefreshCw size={11} className={loading ? "animate-spin" : ""} />
          {preview ? "Refresh" : "Check now"}
        </button>
      </div>

      {error && (
        <div className="flex items-start gap-2 px-3 py-2 rounded-lg text-xs" style={{ background: C.sellBg, color: C.red }}>
          <AlertTriangle size={13} className="shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      {!error && !preview && !loading && (
        <p className="text-xs text-gray-400">See what the engine's next adjustment would do — real live pricing and margin, no orders placed.</p>
      )}

      {preview && (
        <div className="space-y-3">
          <p className="text-xs text-gray-600">{preview.trigger_detail}</p>

          {preview.current_margin != null && (
            <div className="grid grid-cols-3 gap-3">
              <div>
                <div className="text-[10px] text-gray-400 uppercase tracking-wide">Current margin</div>
                <div className="text-sm font-semibold text-gray-700 tabular-nums">{inr(preview.current_margin)}</div>
              </div>
              {preview.triggered && preview.projected_margin != null && (
                <>
                  <div>
                    <div className="text-[10px] text-gray-400 uppercase tracking-wide">Projected margin</div>
                    <div className="text-sm font-semibold text-gray-700 tabular-nums">{inr(preview.projected_margin)}</div>
                  </div>
                  <div>
                    <div className="text-[10px] text-gray-400 uppercase tracking-wide">Change</div>
                    <div className="text-sm font-semibold tabular-nums" style={{ color: sign(preview.margin_delta ?? 0) }}>
                      {withSign(preview.margin_delta ?? 0)}
                    </div>
                  </div>
                </>
              )}
            </div>
          )}

          {preview.triggered && preview.new_legs && preview.new_legs.length > 0 && (
            <div>
              <div className="text-[10px] text-gray-400 uppercase tracking-wide mb-1.5">Would open</div>
              <div className="flex flex-wrap gap-1.5">
                {preview.new_legs.map((leg, i) => (
                  <span key={i} className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-bold"
                    style={leg.action === "SELL" ? { background: C.sellBg, color: C.sellText } : { background: C.buyBg, color: C.buyText }}>
                    {leg.action} {leg.option_type} {leg.strike}
                    {leg.estimated_premium != null && <span className="font-normal opacity-70">@{leg.estimated_premium.toFixed(1)}</span>}
                  </span>
                ))}
              </div>
            </div>
          )}

          {preview.triggered && preview.legs_closed && preview.legs_closed.length > 0 && (
            <div>
              <div className="text-[10px] text-gray-400 uppercase tracking-wide mb-1.5">Would close</div>
              <div className="flex flex-wrap gap-1.5">
                {preview.legs_closed.map((leg, i) => (
                  <span key={i} className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-bold" style={{ background: C.hover, color: C.text }}>
                    {leg.option_type} {leg.strike}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
