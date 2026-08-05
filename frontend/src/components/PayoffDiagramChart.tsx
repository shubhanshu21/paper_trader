import { useRef, useState } from "react";
import { C } from "../lib/format";

interface PayoffCurvePoint {
  price: number;
  pnl: number;
}

interface PayoffLeg {
  strike: number;
  option_type: "CE" | "PE";
  action: "BUY" | "SELL";
  quantity: number;
  current_price: number;
}

interface PayoffDiagramChartProps {
  curve: PayoffCurvePoint[];
  spotPrice: number;
  breakevens: number[];
  height?: number;
  legs?: PayoffLeg[];
  expiry?: string;
  daysRemaining?: number;
  ivShift?: number;
  symbol?: string;
}

function stdNormalCDF(x: number): number {
  const t = 1 / (1 + 0.2316419 * Math.abs(x));
  const d = 0.39894228 * Math.exp(-x * x / 2);
  const p = d * t * (0.31938153 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))));
  return x >= 0 ? 1 - p : p;
}

function blackScholes(S: number, K: number, T: number, r: number, sigma: number, type: "CE" | "PE"): number {
  if (T <= 0) {
    if (type === "CE") return Math.max(0, S - K);
    return Math.max(0, K - S);
  }
  const d1 = (Math.log(S / K) + (r + (sigma * sigma) / 2) * T) / (sigma * Math.sqrt(T));
  const d2 = d1 - sigma * Math.sqrt(T);
  if (type === "CE") {
    return S * stdNormalCDF(d1) - K * Math.exp(-r * T) * stdNormalCDF(d2);
  } else {
    return K * Math.exp(-r * T) * stdNormalCDF(-d2) - S * stdNormalCDF(-d1);
  }
}

function solveIV(S: number, K: number, T: number, r: number, marketPrice: number, type: "CE" | "PE"): number {
  let low = 0.0001, high = 5.0, mid = 0.2;
  for (let i = 0; i < 40; i++) {
    mid = (low + high) / 2;
    const price = blackScholes(S, K, T, r, mid, type);
    if (Math.abs(price - marketPrice) < 0.0001) return mid;
    if (price > marketPrice) high = mid;
    else low = mid;
  }
  return mid;
}

const fmt0 = (n: number) => Math.round(n).toLocaleString("en-IN");

/** "Nice" round tick step for an axis range (1/2/5 x a power of 10 — the same rule every charting library uses) so labels land on clean numbers like 21,000 / 22,500, not 21,083.33. */
function niceStep(range: number, targetTicks: number): number {
  const rough = range / Math.max(targetTicks, 1);
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const norm = rough / mag;
  const step = norm >= 5 ? 5 : norm >= 2 ? 2 : 1;
  return step * mag;
}

function ticksBetween(min: number, max: number, targetTicks: number): number[] {
  const step = niceStep(max - min, targetTicks) || 1;
  const start = Math.ceil(min / step) * step;
  const out: number[] = [];
  for (let v = start; v <= max + step * 1e-6; v += step) out.push(Math.round(v * 100) / 100);
  return out;
}

/** BUY/SELL CE/PE @strike, one per leg, joined — the compact strategy summary Sensibull/Opstra-style payoff cards show as a subtitle under the title. */
function legsSummary(legs: PayoffLeg[]): string {
  return legs
    .map((l) => `${l.action} ${l.strike} ${l.option_type} @₹${l.current_price.toFixed(0)}`)
    .join("  +  ");
}

export default function PayoffDiagramChart({ curve, spotPrice, breakevens, height = 300, legs, expiry, daysRemaining, ivShift = 0, symbol }: PayoffDiagramChartProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  if (curve.length < 2) return null;

  const w = 680, padX = 62, padTop = 46, padBottom = 46;
  const prices = curve.map((p) => p.price);
  const pnls = curve.map((p) => p.pnl);
  const minPrice = Math.min(...prices), maxPrice = Math.max(...prices);
  const minPnl = Math.min(0, ...pnls), maxPnl = Math.max(0, ...pnls);
  const pnlPad = (maxPnl - minPnl || 1) * 0.08; // a little headroom so the curve/labels never touch the frame
  const yMin = minPnl - pnlPad, yMax = maxPnl + pnlPad;
  const pnlRange = yMax - yMin || 1;

  const x = (price: number) => padX + ((price - minPrice) / (maxPrice - minPrice || 1)) * (w - padX * 2);
  const y = (pnl: number) => padTop + (1 - (pnl - yMin) / pnlRange) * (height - padTop - padBottom);
  const yZero = y(0);

  const linePoints = curve.map((p) => `${x(p.price)},${y(p.pnl)}`).join(" ");
  const areaPoints = `${x(curve[0].price)},${yZero} ${linePoints} ${x(curve[curve.length - 1].price)},${yZero}`;
  const spotX = x(Math.min(Math.max(spotPrice, minPrice), maxPrice));

  const uid = `payoff-${Math.round(spotPrice * 100)}-${Math.round(minPnl)}`;

  const calculateT0Pnl = (price: number) => {
    if (!legs || !expiry) return 0;
    const r = 0.065;
    const totalDays = Math.max((new Date(expiry).getTime() - new Date().getTime()) / (1000 * 3600 * 24), 1);
    const targetDays = daysRemaining !== undefined ? daysRemaining : totalDays;
    const T_target = Math.max(targetDays, 0.001) / 365.0;
    const T_initial = totalDays / 365.0;

    let totalPnl = 0;
    for (const leg of legs) {
      const iv = solveIV(spotPrice, leg.strike, T_initial, r, leg.current_price, leg.option_type);
      const adjustedIv = Math.max(0.01, iv * (1 + ivShift));
      const theoreticalPrice = blackScholes(price, leg.strike, T_target, r, adjustedIv, leg.option_type);
      const sign = leg.action === "BUY" ? 1 : -1;
      const legPnl = (theoreticalPrice - leg.current_price) * leg.quantity * sign;
      totalPnl += legPnl;
    }
    return totalPnl;
  };

  const hasT0 = !!(legs && expiry);
  const t0Points = curve.map((p) => `${x(p.price)},${y(calculateT0Pnl(p.price))}`).join(" ");

  const nearestIndexForClientX = (clientX: number): number => {
    const rect = svgRef.current!.getBoundingClientRect();
    const frac = (clientX - rect.left) / rect.width;
    const svgX = frac * w;
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
  const hoverT0Y = hover && hasT0 ? y(calculateT0Pnl(hover.price)) : 0;
  const tooltipLeftPct = hover ? (hoverX / w) * 100 : 0;
  const tooltipFlip = tooltipLeftPct > 62;

  const yTicks = ticksBetween(yMin, yMax, 5).filter((t) => t !== 0);
  const xTicks = ticksBetween(minPrice, maxPrice, 6);
  const relevantBreakevens = breakevens.filter((b) => b >= minPrice && b <= maxPrice).sort((a, b) => a - b);
  const beLabel = (i: number) => (relevantBreakevens.length === 2 ? (i === 0 ? "Lower B/E" : "Upper B/E") : relevantBreakevens.length === 1 ? "B/E" : `B/E ${i + 1}`);

  return (
    <div className="w-full select-none rounded-xl bg-white" style={{ border: `1px solid ${C.border2}` }}>
      {/* Title + subtitle + legend — matches the standard Sensibull/Opstra payoff-card header */}
      <div className="pt-4 pb-1 text-center">
        <div className="text-[15px] font-bold text-gray-800">{symbol ? `${symbol} Payoff` : "Payoff Diagram"}</div>
        {legs && legs.length > 0 && (
          <div className="text-[11px] text-gray-400 mt-0.5 px-4">{legsSummary(legs)}</div>
        )}
        <div className="flex items-center justify-center gap-4 mt-2.5 text-[11px] font-medium text-gray-500">
          <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-sm inline-block" style={{ background: "#d7f2df" }} /> Profit zone</span>
          <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-sm inline-block" style={{ background: "#fbdcdb" }} /> Loss zone</span>
          <span className="flex items-center gap-1.5">
            <svg width="16" height="8"><line x1="1" y1="4" x2="15" y2="4" stroke={C.blue} strokeWidth="2.5" strokeLinecap="round" /></svg> P&amp;L
          </span>
          {hasT0 && (
            <span className="flex items-center gap-1.5">
              <svg width="16" height="8"><line x1="1" y1="4" x2="15" y2="4" stroke="#8b5cf6" strokeWidth="2" strokeDasharray="3,2" strokeLinecap="round" /></svg> Pre-Expiry (T+0)
            </span>
          )}
        </div>
      </div>

      <div className="relative">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${w} ${height}`}
          className="w-full block"
          style={{ height, cursor: "crosshair" }}
          preserveAspectRatio="none"
          onMouseMove={handleMove}
          onMouseLeave={() => setHoverIdx(null)}
          onTouchStart={handleMove}
          onTouchMove={handleMove}
          onTouchEnd={() => setHoverIdx(null)}
        >
          <defs>
            <clipPath id={`${uid}-profit`}><rect x={0} y={0} width={w} height={yZero} /></clipPath>
            <clipPath id={`${uid}-loss`}><rect x={0} y={yZero} width={w} height={height - yZero} /></clipPath>
          </defs>

          {/* Horizontal gridlines at nice round P&L values */}
          {yTicks.map((t) => (
            <g key={t}>
              <line x1={padX} y1={y(t)} x2={w - padX} y2={y(t)} stroke={C.border} strokeWidth={1} />
              <text x={padX - 8} y={y(t) + 3.5} fontSize={10} textAnchor="end" fill={C.muted}>{fmt0(t)}</text>
            </g>
          ))}

          {/* Profit / loss zone fills — flat, saturated tint matching the legend swatches exactly */}
          <polygon points={areaPoints} fill="#d7f2df" clipPath={`url(#${uid}-profit)`} />
          <polygon points={areaPoints} fill="#fbdcdb" clipPath={`url(#${uid}-loss)`} />

          {/* Zero line */}
          <line x1={padX} y1={yZero} x2={w - padX} y2={yZero} stroke={C.text} strokeWidth={1} opacity={0.4} />
          <text x={padX - 8} y={yZero + 3.5} fontSize={10} textAnchor="end" fill={C.muted}>0</text>

          {/* Breakeven markers — orange dashed line + labeled box above the plot, exactly like the reference */}
          {relevantBreakevens.map((b, i) => (
            <g key={b}>
              <line x1={x(b)} y1={padTop - 4} x2={x(b)} y2={height - padBottom} stroke={C.orange} strokeWidth={1.5} strokeDasharray="5,4" />
              <g transform={`translate(${x(b)}, ${padTop - 20})`}>
                <rect x={-52} y={-11} width={104} height={22} rx={5} fill="#fff" stroke={C.orange} strokeWidth={1.25} />
                <text x={0} y={-1} fontSize={9} fontWeight={600} textAnchor="middle" fill={C.orange}>{beLabel(i)}</text>
                <text x={0} y={9} fontSize={9.5} fontWeight={700} textAnchor="middle" fill={C.orange}>{fmt0(b)}</text>
              </g>
            </g>
          ))}

          {/* Spot price — subtle grey reference line, deliberately understated next to the orange breakevens */}
          <line x1={spotX} y1={padTop - 4} x2={spotX} y2={height - padBottom} stroke={C.text} strokeWidth={1} strokeDasharray="2,3" opacity={0.45} />
          <g transform={`translate(${spotX}, ${height - padBottom + 14})`}>
            <rect x={-40} y={-3} width={80} height={16} rx={4} fill={C.text} opacity={0.85} />
            <text x={0} y={9} fontSize={9} fontWeight={600} textAnchor="middle" fill="#fff">Spot {fmt0(spotPrice)}</text>
          </g>

          {/* Expiry payoff curve (the real, piecewise-linear shape — kinks at each strike, never smoothed) */}
          <polyline points={linePoints} fill="none" stroke={C.blue} strokeWidth={2.5} strokeLinejoin="round" strokeLinecap="round" />

          {/* Pre-expiry (T+0) theoretical curve */}
          {hasT0 && (
            <polyline points={t0Points} fill="none" stroke="#8b5cf6" strokeWidth={1.75} strokeDasharray="5,3.5" strokeLinejoin="round" strokeLinecap="round" />
          )}

          {/* Hover crosshair */}
          {hover && (
            <g pointerEvents="none">
              <line x1={hoverX} y1={padTop - 4} x2={hoverX} y2={height - padBottom} stroke={C.muted} strokeWidth={1} strokeDasharray="3,3" />
              {hasT0 && <circle cx={hoverX} cy={hoverT0Y} r={3} fill="#fff" stroke="#8b5cf6" strokeWidth={2} />}
              <circle cx={hoverX} cy={hoverY} r={4} fill={C.blue} stroke="#fff" strokeWidth={1.5} />
            </g>
          )}

          {/* X-axis price ticks */}
          {xTicks.map((t) => (
            <text key={t} x={x(t)} y={height - padBottom + 30} fontSize={10} textAnchor="middle" fill={C.muted}>{fmt0(t)}</text>
          ))}

          {/* Axis titles */}
          <text x={(padX + (w - padX)) / 2} y={height - 6} fontSize={10.5} fontWeight={600} textAnchor="middle" fill={C.text}>
            {symbol ? `${symbol} Price at Expiry (Rs.)` : "Price at Expiry (Rs.)"}
          </text>
          <text x={0} y={0} fontSize={10.5} fontWeight={600} textAnchor="middle" fill={C.text} transform={`translate(14, ${(padTop + (height - padBottom)) / 2}) rotate(-90)`}>
            Profit / Loss (Rs.)
          </text>
        </svg>

        {hover && (
          <div
            className="absolute top-2 rounded-xl overflow-hidden shadow-lg text-[11px] pointer-events-none"
            style={{
              left: `${tooltipLeftPct}%`,
              transform: tooltipFlip ? "translateX(-100%)" : "translateX(0%)",
              marginLeft: tooltipFlip ? -10 : 10,
              background: "#fff", border: `1px solid ${C.border2}`, whiteSpace: "nowrap", zIndex: 10,
              borderLeft: `3px solid ${hover.pnl >= 0 ? C.green : C.red}`,
            }}
          >
            <div className="px-3 py-2">
              <div className="font-bold text-gray-800 text-[13px]">Rs. {fmt0(hover.price)}</div>
              <div className="mt-1.5 flex items-center justify-between gap-4">
                <span className="text-[10px] text-gray-400 flex items-center gap-1">
                  <span className="w-2 h-2 rounded-full inline-block" style={{ background: C.blue }} /> At Expiry
                </span>
                <span className="font-bold" style={{ color: hover.pnl >= 0 ? C.green : C.red }}>
                  {hover.pnl >= 0 ? "+Rs. " : "-Rs. "}{fmt0(Math.abs(hover.pnl))}
                </span>
              </div>
              {hasT0 && (
                <div className="mt-1 flex items-center justify-between gap-4">
                  <span className="text-[10px] text-gray-400 flex items-center gap-1">
                    <span className="w-2 h-2 rounded-full inline-block border-2" style={{ borderColor: "#8b5cf6", background: "#fff" }} /> Pre-Expiry
                  </span>
                  <span className="font-bold" style={{ color: "#8b5cf6" }}>
                    {calculateT0Pnl(hover.price) >= 0 ? "+Rs. " : "-Rs. "}{fmt0(Math.abs(calculateT0Pnl(hover.price)))}
                  </span>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
