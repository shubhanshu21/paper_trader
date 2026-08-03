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

export default function PayoffDiagramChart({ curve, spotPrice, breakevens, height = 220, legs, expiry, daysRemaining, ivShift = 0 }: PayoffDiagramChartProps) {
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

  const t0Points = curve.map((p) => {
    const t0Pnl = calculateT0Pnl(p.price);
    return `${x(p.price)},${y(t0Pnl)}`;
  }).join(" ");

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
  const tooltipLeftPct = hover ? (hoverX / w) * 100 : 0;
  const tooltipFlip = tooltipLeftPct > 65; 

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

        <line x1={padX} y1={yZero} x2={w - padX} y2={yZero} stroke={C.border2} strokeWidth={1} />

        <line x1={spotX} y1={padY} x2={spotX} y2={height - padY} stroke={C.blue} strokeWidth={1} strokeDasharray="4,3" />
        <text x={spotX} y={padY - 4} fontSize={9} textAnchor="middle" fill={C.blue}>Spot ₹{spotPrice.toFixed(0)}</text>

        {breakevens.filter((b) => b >= minPrice && b <= maxPrice).map((b) => (
          <g key={b}>
            <line x1={x(b)} y1={padY} x2={x(b)} y2={height - padY} stroke={C.faint} strokeWidth={1} strokeDasharray="2,3" />
            <text x={x(b)} y={height - padY + 12} fontSize={9} textAnchor="middle" fill={C.muted}>₹{b.toFixed(0)}</text>
          </g>
        ))}

        <polygon points={areaPoints} fill={C.green} opacity={0.15} clipPath={`url(#${clipId}-profit)`} />
        <polygon points={areaPoints} fill={C.red} opacity={0.15} clipPath={`url(#${clipId}-loss)`} />

        <polyline points={linePoints} fill="none" stroke={C.text} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />

        {legs && expiry && (
          <polyline
            points={t0Points}
            fill="none"
            stroke={C.blue}
            strokeWidth={1.5}
            strokeDasharray="4,3"
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        )}

        {hover && (
          <g pointerEvents="none">
            <line x1={hoverX} y1={padY} x2={hoverX} y2={height - padY} stroke={C.muted} strokeWidth={1} strokeDasharray="3,3" />
            <circle cx={hoverX} cy={hoverY} r={3.5} fill={hover.pnl >= 0 ? C.green : C.red} stroke="#fff" strokeWidth={1.5} />
          </g>
        )}

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
          <div className="text-[10px] text-gray-400">At Expiry:</div>
          <div className="font-bold" style={{ color: hover.pnl >= 0 ? C.green : C.red }}>
            {hover.pnl >= 0 ? "+₹" : "-₹"}{Math.abs(hover.pnl).toFixed(2)}
          </div>
          {legs && expiry && (
            <>
              <div className="text-[10px] text-gray-400 mt-1">Theoretical (T+0):</div>
              <div className="font-bold text-blue-600">
                {calculateT0Pnl(hover.price) >= 0 ? "+₹" : "-₹"}{Math.abs(calculateT0Pnl(hover.price)).toFixed(2)}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
