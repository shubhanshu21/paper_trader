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
 */
export default function PayoffDiagramChart({ curve, spotPrice, breakevens, height = 220 }: PayoffDiagramChartProps) {
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

  return (
    <div className="w-full">
      <svg viewBox={`0 0 ${w} ${height}`} className="w-full" style={{ height }} preserveAspectRatio="none">
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

        {/* Y-axis labels */}
        <text x={padX - 6} y={y(maxPnl) + 3} fontSize={9} textAnchor="end" fill={C.muted}>₹{maxPnl.toFixed(0)}</text>
        <text x={padX - 6} y={y(minPnl) + 3} fontSize={9} textAnchor="end" fill={C.muted}>₹{minPnl.toFixed(0)}</text>
      </svg>
    </div>
  );
}
