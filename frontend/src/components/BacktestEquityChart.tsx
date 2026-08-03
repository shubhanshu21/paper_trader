import { useEffect, useRef } from "react";
import { createChart, IChartApi, LineSeries, AreaSeries } from "lightweight-charts";
import { C } from "../lib/format";

interface BacktestEquityChartProps {
  equityCurve: number[];  // compounded ₹ curve, one point per cycle, chronological
  dates: string[];        // same length — each cycle's exit_date ('YYYY-MM-DD')
  height?: number;
}

/**
 * Chart-display-only normalization: lightweight-charts requires strictly
 * ascending, unique time points. Cycles are chronological by entry_date
 * (guaranteed server-side — see routes_custom_strategies.py's
 * _run_backtest_symbols), but a multi-symbol backtest's exit_dates can
 * collide or run out of that same order (two symbols' cycles overlap).
 * Bumping a colliding date forward by a day keeps every point visually
 * distinct without touching the underlying data/order.
 */
function normalizeAscendingDates(dates: string[]): string[] {
  const out: string[] = [];
  let prev: string | null = null;
  for (const d of dates) {
    let cur = d;
    if (prev !== null && cur <= prev) {
      const next = new Date(prev + "T00:00:00");
      next.setDate(next.getDate() + 1);
      cur = next.toISOString().slice(0, 10);
    }
    out.push(cur);
    prev = cur;
  }
  return out;
}

export default function BacktestEquityChart({ equityCurve, dates, height = 260 }: BacktestEquityChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!containerRef.current || equityCurve.length < 2) return;

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height,
      layout: { background: { color: "#ffffff" }, textColor: "#333333" },
      grid: { vertLines: { color: "#f0f0f0" }, horzLines: { color: "#f0f0f0" } },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: "#cccccc" },
      timeScale: { borderColor: "#cccccc", timeVisible: false },
    });
    chartRef.current = chart;

    const times = normalizeAscendingDates(dates);

    // Top pane (0, the default): compounded equity curve.
    const equitySeries = chart.addSeries(LineSeries, {
      color: C.blue, lineWidth: 2, priceFormat: { type: "custom", formatter: (v: number) => `₹${v.toFixed(0)}` },
    }, 0);
    equitySeries.setData(times.map((t, i) => ({ time: t, value: equityCurve[i] })));

    // Bottom pane: underwater drawdown %, relative to the running peak.
    let peak = equityCurve[0];
    const drawdownPct = equityCurve.map((v) => {
      peak = Math.max(peak, v);
      return peak > 0 ? -((peak - v) / peak) * 100 : 0;
    });
    const drawdownSeries = chart.addSeries(AreaSeries, {
      lineColor: C.red, topColor: "rgba(223,81,76,0.25)", bottomColor: "rgba(223,81,76,0.02)", lineWidth: 1,
      priceFormat: { type: "custom", formatter: (v: number) => `${v.toFixed(1)}%` },
    }, 1);
    drawdownSeries.setData(times.map((t, i) => ({ time: t, value: drawdownPct[i] })));

    chart.panes()[1]?.setHeight(Math.round(height * 0.35));

    const handleResize = () => {
      if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth });
    };
    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
  }, [equityCurve, dates, height]);

  if (equityCurve.length < 2) return null;

  return (
    <div className="w-full">
      <div className="flex items-center gap-4 mb-1.5 text-[11px]">
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-0.5 rounded-full" style={{ background: C.blue }} /> Equity (₹)</span>
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-sm" style={{ background: "rgba(223,81,76,0.4)" }} /> Drawdown</span>
      </div>
      <div ref={containerRef} className="w-full border rounded-lg overflow-hidden" style={{ borderColor: C.border2 }} />
    </div>
  );
}
