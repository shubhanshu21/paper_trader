import { useRef, useState } from "react";
import { C } from "./Common";

interface PayoffCurvePoint {
  price: number;
  pnl: number;
}

interface PayoffDiagramChartProps {
  curve: PayoffCurvePoint[];
  spotPrice: number;
  breakevens: number[];
  height?: number;
}

/**
 * Hand-rolled SVG, not lightweight-charts (see TradingChart.tsx/
 * BacktestEquityChart.tsx for that library's usage elsewhere) —
 * lightweight-charts' x-axis is fundamentally time-based; a payoff
 * diagram's x-axis is the underlying PRICE at expiry, not time, so it's
 * the wrong tool here. Same hand-rolled-SVG approach this codebase
 * already uses for EquityCurveChart in StrategiesView.tsx.
 *
 * The profit/loss region is split into two colors (green above zero, red
 * below) via one clipPath per half rather than finding each zero-crossing
 * manually — the SAME area-under-curve polygon is filled twice, once
 * clipped to the y<0 half and once to the y>=0 half, which is correct
 * regardless of how many times a multi-leg strategy's piecewise-linear
 * payoff crosses zero (an iron condor crosses twice, a naked leg once).
 *
 * Interactive: hovering/touching the plot snaps a crosshair to the
 * nearest computed curve point and shows its exact price/P&L in a
 * floating tooltip — mouse position arrives in on-screen pixels
 * (clientX/clientY) but the curve is drawn in the SVG's own viewBox
 * units, so every pointer event is first converted via the SVG
 * element's live bounding rect (works under preserveAspectRatio="none"
 * responsive scaling, and needs no redraw-time coordinate caching).
 */
export default function PayoffDiagramChart({ curve, spotPrice, breakevens, height = 220 }: PayoffDiagramChartProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  if (curve.length < 2) return null;

  const w = 640, padX = 46, padY = 16;
  const prices = curve.map((p) => p.price);
  const pnls = curve.map((p) => p.pnl);
  const minPrice = Math.min(...prices), maxPrice = Math.max(...prices);
  const minPnl = Math.min(0, ...pnls), maxPnl = Math.max(0, ...pnls);
  const pnlRange = maxPnl - minPnl || 1;

  const x = (price: number) => padX + ((price - minPrice) / (maxPrice - minPrice || 1)) * (w - padX * 2);
  const y = (pnl: number) => padY + (1 - (pnl - minPnl) / pnlRange) * (height - padY * 2);
  const yZero = y(0);

  const linePoints = curve.map((p) => `${x(p.price)},${y(p.pnl)}`).join(" ");
  const areaPoints = `${x(curve[0].price)},${yZero} ${linePoints} ${x(curve[curve.length - 1].price)},${yZero}`;
  const spotX = x(Math.min(Math.max(spotPrice, minPrice), maxPrice));

  const clipId = `payoff-clip-${Math.round(spotPrice * 100)}`;

  const nearestIndexForClientX = (clientX: number): number => {
    const rect = svgRef.current!.getBoundingClientRect();
    const frac = (clientX - rect.left) / rect.width; // 0..1 across the rendered SVG
    const svgX = frac * w; // back into viewBox units
    const price = minPrice + ((svgX - padX) / (w - padX * 2)) * (maxPrice - minPrice || 1);
    let nearest = 0, best = Infinity;
    for (let i = 0; i < curve.length; i++) {
      const d = Math.abs(curve[i].price - price);
      if (d < best) { best = d; nearest = i; }
    }
    return nearest;
  };

  const handleMove = (e: React.MouseEvent<SVGSVGElement> | React.TouchEvent<SVGSVGElement>) => {
    const clientX = "touches" in e ? e.touches[0]?.clientX : e.clientX;
    if (clientX == null) return;
    setHoverIdx(nearestIndexForClientX(clientX));
  };

  const hover = hoverIdx != null ? curve[hoverIdx] : null;
  const hoverX = hover ? x(hover.price) : 0;
  const hoverY = hover ? y(hover.pnl) : 0;
  const tooltipLeftPct = hover ? (hoverX / w) * 100 : 0;
  const tooltipFlip = tooltipLeftPct > 65; // keep the tooltip on-screen near the right edge

  return (
    <div className="w-full relative select-none">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${w} ${height}`}
        className="w-full"
        style={{ height, cursor: "crosshair" }}
        preserveAspectRatio="none"
        onMouseMove={handleMove}
        onMouseLeave={() => setHoverIdx(null)}
        onTouchStart={handleMove}
        onTouchMove={handleMove}
        onTouchEnd={() => setHoverIdx(null)}
      >
        <defs>
          <clipPath id={`${clipId}-profit`}><rect x={0} y={0} width={w} height={yZero} /></clipPath>
          <clipPath id={`${clipId}-loss`}><rect x={0} y={yZero} width={w} height={height - yZero} /></clipPath>
        </defs>

        {/* Zero P&L baseline */}
        <line x1={padX} y1={yZero} x2={w - padX} y2={yZero} stroke={C.border2} strokeWidth={1} />

        {/* Current spot marker */}
        <line x1={spotX} y1={padY} x2={spotX} y2={height - padY} stroke={C.blue} strokeWidth={1} strokeDasharray="4,3" />
        <text x={spotX} y={padY - 4} fontSize={9} textAnchor="middle" fill={C.blue}>Spot ₹{spotPrice.toFixed(0)}</text>

        {/* Breakeven markers */}
        {breakevens.filter((b) => b >= minPrice && b <= maxPrice).map((b) => (
          <g key={b}>
            <line x1={x(b)} y1={padY} x2={x(b)} y2={height - padY} stroke={C.faint} strokeWidth={1} strokeDasharray="2,3" />
            <text x={x(b)} y={height - padY + 12} fontSize={9} textAnchor="middle" fill={C.muted}>₹{b.toFixed(0)}</text>
          </g>
        ))}

        {/* Profit region (green) and loss region (red) — same polygon, clipped twice */}
        <polygon points={areaPoints} fill={C.green} opacity={0.15} clipPath={`url(#${clipId}-profit)`} />
        <polygon points={areaPoints} fill={C.red} opacity={0.15} clipPath={`url(#${clipId}-loss)`} />

        <polyline points={linePoints} fill="none" stroke={C.text} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />

        {/* Hover crosshair */}
        {hover && (
          <g pointerEvents="none">
            <line x1={hoverX} y1={padY} x2={hoverX} y2={height - padY} stroke={C.muted} strokeWidth={1} strokeDasharray="3,3" />
            <circle cx={hoverX} cy={hoverY} r={3.5} fill={hover.pnl >= 0 ? C.green : C.red} stroke="#fff" strokeWidth={1.5} />
          </g>
        )}

        {/* Y-axis labels */}
        <text x={padX - 6} y={y(maxPnl) + 3} fontSize={9} textAnchor="end" fill={C.muted}>₹{maxPnl.toFixed(0)}</text>
        <text x={padX - 6} y={y(minPnl) + 3} fontSize={9} textAnchor="end" fill={C.muted}>₹{minPnl.toFixed(0)}</text>
      </svg>

      {hover && (
        <div
          className="absolute top-1 px-2.5 py-1.5 rounded-lg shadow-lg text-[11px] pointer-events-none"
          style={{
            left: `${tooltipLeftPct}%`,
            transform: tooltipFlip ? "translateX(-100%)" : "translateX(0%)",
            marginLeft: tooltipFlip ? -8 : 8,
            background: "#fff", border: `1px solid ${C.border2}`, whiteSpace: "nowrap", zIndex: 10,
          }}
        >
          <div className="font-semibold text-gray-700">₹{hover.price.toFixed(2)}</div>
          <div className="font-bold" style={{ color: hover.pnl >= 0 ? C.green : C.red }}>
            {hover.pnl >= 0 ? "+" : ""}₹{hover.pnl.toFixed(2)}
          </div>
        </div>
      )}
    </div>
  );
}
