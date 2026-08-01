import { useCallback, useMemo, useState } from "react";
import {
  ReactFlow, ReactFlowProvider, Background, Controls, Handle, Position,
  type Node, type Edge, type NodeProps, type OnNodesChange, type NodeChange,
  BackgroundVariant,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Trash2, Plus, Zap, TrendingDown, Percent } from "lucide-react";
import { C } from "./Common";
import { PortalDropdown } from "./PortalDropdown";
import {
  type StrikeMode, type ExpiryModeOverride, type EntryMode, type ConditionType,
  type LegForm, type ConditionForm, strikeLabel,
} from "./strategyBuilderTypes";

// ---------------------------------------------------------------------------
// Node components
// ---------------------------------------------------------------------------
const cardStyle: React.CSSProperties = {
  border: `1.5px solid ${C.border2}`, borderRadius: 12, background: "#fff",
  boxShadow: "0 1px 3px rgba(0,0,0,0.06)", width: 240, fontSize: 12,
};
const headerStyle = (bg: string, fg: string): React.CSSProperties => ({
  padding: "6px 10px", borderRadius: "10px 10px 0 0", background: bg, color: fg,
  fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.4,
});
const fieldLabel: React.CSSProperties = { fontSize: 10, color: C.muted, marginBottom: 2, display: "block" };
const numInput = "w-full px-2 py-1 border rounded text-xs";

function SymbolNode({ data }: NodeProps) {
  const symbols = (data.symbols as string[]) ?? [];
  return (
    <div style={cardStyle}>
      <div style={headerStyle("#fff7ed", C.orange)}>Underlying</div>
      <div className="p-2.5">
        {symbols.length === 0
          ? <div className="text-xs text-gray-400 italic">No symbols selected yet (Step 1)</div>
          : <div className="flex flex-wrap gap-1">{symbols.map((s) => (
              <span key={s} className="px-2 py-0.5 rounded-full text-[10px] font-bold bg-orange-50 text-orange-700 border border-orange-200">{s}</span>
            ))}</div>}
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function EntryNode({ data }: NodeProps) {
  const mode = data.entryMode as EntryMode;
  const onEntryModeChange = data.onEntryModeChange as (m: EntryMode) => void;
  const entryTime = data.entryTime as string;
  const onEntryTimeChange = data.onEntryTimeChange as (v: string) => void;
  const condition = data.condition as ConditionForm;
  const onConditionChange = data.onConditionChange as (patch: Partial<ConditionForm>) => void;

  return (
    <div style={cardStyle}>
      <Handle type="target" position={Position.Left} />
      <div style={headerStyle("#eef2ff", "#4338ca")}>Entry</div>
      <div className="p-2.5 space-y-2">
        <PortalDropdown
          value={mode}
          onChange={(v) => onEntryModeChange(v as EntryMode)}
          options={[
            { value: "IMMEDIATE", label: "Immediately" },
            { value: "AT_TIME", label: "At a specific time" },
            { value: "CONDITIONAL", label: "On a condition" },
          ]}
        />
        {mode === "AT_TIME" && (
          <div>
            <label style={fieldLabel}>Entry time (IST)</label>
            <input type="time" value={entryTime} onChange={(e) => onEntryTimeChange(e.target.value)} className={numInput} />
          </div>
        )}
        {mode === "CONDITIONAL" && (
          <div className="space-y-2 pt-1 border-t" style={{ borderColor: C.border }}>
            <div className="flex items-center gap-1 text-[10px] font-semibold" style={{ color: "#4338ca" }}>
              <Zap size={11} /> Condition
            </div>
            <PortalDropdown
              value={condition.type}
              onChange={(v) => onConditionChange({ type: v as ConditionType })}
              options={[
                { value: "MA_CROSSOVER", label: "Moving-average crossover" },
                { value: "IV_RANK", label: "IV rank" },
              ]}
            />
            {condition.type === "MA_CROSSOVER" ? (
              <div className="grid grid-cols-2 gap-1.5">
                <div>
                  <label style={fieldLabel}>Period (days)</label>
                  <input type="number" min={2} value={condition.ma_period_days}
                    onChange={(e) => onConditionChange({ ma_period_days: e.target.value })} className={numInput} />
                </div>
                <div>
                  <label style={fieldLabel}>Price is</label>
                  <PortalDropdown value={condition.ma_direction} onChange={(v) => onConditionChange({ ma_direction: v as "ABOVE" | "BELOW" })}
                    options={[{ value: "ABOVE", label: "Above MA" }, { value: "BELOW", label: "Below MA" }]} />
                </div>
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-1.5">
                <div>
                  <label style={fieldLabel}>IV rank is</label>
                  <PortalDropdown value={condition.iv_operator} onChange={(v) => onConditionChange({ iv_operator: v as "ABOVE" | "BELOW" })}
                    options={[{ value: "ABOVE", label: "Above" }, { value: "BELOW", label: "Below" }]} />
                </div>
                <div>
                  <label style={fieldLabel}>Threshold</label>
                  <input type="number" min={0} max={100} value={condition.iv_threshold}
                    onChange={(e) => onConditionChange({ iv_threshold: e.target.value })} className={numInput} />
                </div>
              </div>
            )}
            <p className="text-[10px] text-gray-400 leading-snug">IV rank needs ~30+ days of accumulated history for this symbol before it can trigger — see the strategy's status page once deployed.</p>
          </div>
        )}
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function LegNode({ data }: NodeProps) {
  const leg = data.leg as LegForm;
  const idx = data.idx as number;
  const onUpdate = data.onUpdate as (patch: Partial<LegForm>) => void;
  const onRemove = data.onRemove as (() => void) | null;
  const hasCalendarSpread = data.hasCalendarSpread as boolean;

  return (
    <div style={{ ...cardStyle, width: 260 }}>
      <Handle type="target" position={Position.Left} />
      <div className="flex items-center justify-between" style={headerStyle(leg.action === "BUY" ? "#e2f2ff" : "#fff1f0", leg.action === "BUY" ? C.buyText : C.sellText)}>
        <span>Leg {idx + 1}</span>
        {onRemove && <button onClick={onRemove} className="hover:opacity-70"><Trash2 size={12} /></button>}
      </div>
      <div className="p-2.5 space-y-2">
        <div className="grid grid-cols-2 gap-1.5">
          <div className="flex rounded overflow-hidden border" style={{ borderColor: C.border2 }}>
            {(["BUY", "SELL"] as const).map((a) => (
              <button key={a} onClick={() => onUpdate({ action: a })}
                className={`flex-1 py-1 text-[11px] font-semibold ${leg.action === a ? (a === "BUY" ? "bg-blue-500 text-white" : "bg-red-500 text-white") : "bg-white text-gray-600"}`}>
                {a}
              </button>
            ))}
          </div>
          <div className="flex rounded overflow-hidden border" style={{ borderColor: C.border2 }}>
            {(["CE", "PE"] as const).map((o) => (
              <button key={o} onClick={() => onUpdate({ option_type: o })}
                className={`flex-1 py-1 text-[11px] font-semibold ${leg.option_type === o ? "bg-orange-500 text-white" : "bg-white text-gray-600"}`}>
                {o}
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-1.5">
          <div>
            <label style={fieldLabel}>Strike</label>
            <PortalDropdown value={leg.strike_mode} onChange={(v) => onUpdate({ strike_mode: v as StrikeMode })}
              options={[
                { value: "ATM", label: "ATM" }, { value: "OTM_PERCENT", label: "% OTM" },
                { value: "OTM_POINTS", label: "Points OTM" }, { value: "FIXED", label: "Exact strike" },
              ]} />
          </div>
          {leg.strike_mode !== "ATM" ? (
            <div>
              <label style={fieldLabel}>{leg.strike_mode === "FIXED" ? "Strike price" : "Distance"}</label>
              <input type="number" value={leg.strike_value} onChange={(e) => onUpdate({ strike_value: e.target.value })} className={numInput} />
            </div>
          ) : <div className="flex items-end pb-1 text-[10px] text-gray-400 italic">{strikeLabel(leg)}</div>}
        </div>

        <div>
          <label style={fieldLabel}>Sizing</label>
          <div className="flex rounded overflow-hidden border mb-1" style={{ borderColor: C.border2 }}>
            {(["LOTS", "RISK_PCT"] as const).map((m) => (
              <button key={m} onClick={() => onUpdate({ sizing_mode: m })}
                className={`flex-1 py-1 text-[11px] font-semibold flex items-center justify-center gap-1 ${leg.sizing_mode === m ? "bg-gray-700 text-white" : "bg-white text-gray-600"}`}>
                {m === "RISK_PCT" && <Percent size={9} />}{m === "LOTS" ? "Lots" : "Risk %"}
              </button>
            ))}
          </div>
          {leg.sizing_mode === "LOTS" ? (
            <input type="number" min={1} value={leg.lots} onChange={(e) => onUpdate({ lots: parseInt(e.target.value) || 1 })} className={numInput} />
          ) : (
            <input type="number" min={0.1} step={0.1} value={leg.risk_pct} placeholder="% of available capital"
              onChange={(e) => onUpdate({ risk_pct: e.target.value })} className={numInput} />
          )}
        </div>

        {hasCalendarSpread && (
          <div>
            <label style={fieldLabel}>This leg's expiry</label>
            <PortalDropdown value={leg.expiry_mode || "__default"} onChange={(v) => onUpdate({ expiry_mode: (v === "__default" ? "" : v) as ExpiryModeOverride })}
              options={[
                { value: "__default", label: "Strategy default" },
                { value: "WEEKLY", label: "Weekly (own cycle)" },
                { value: "MONTHLY", label: "Monthly (own cycle)" },
              ]} />
          </div>
        )}

        <div className="pt-1.5 border-t space-y-1.5" style={{ borderColor: C.border }}>
          <div className="text-[10px] font-semibold text-gray-500">Per-leg exit (overrides the strategy's combined exit)</div>
          <div className="grid grid-cols-2 gap-1.5">
            <input type="number" placeholder="TP %" value={leg.leg_take_profit_pct}
              onChange={(e) => onUpdate({ leg_take_profit_pct: e.target.value })} className={numInput} />
            <input type="number" placeholder="SL %" value={leg.leg_stop_loss_pct}
              onChange={(e) => onUpdate({ leg_stop_loss_pct: e.target.value })} className={numInput} />
          </div>
          <label className="flex items-center gap-1.5 text-[11px] text-gray-600 cursor-pointer">
            <input type="checkbox" checked={leg.trailing_enabled} onChange={(e) => onUpdate({ trailing_enabled: e.target.checked })} />
            <TrendingDown size={11} /> Trailing stop
          </label>
          {leg.trailing_enabled && (
            <div className="grid grid-cols-2 gap-1.5">
              <input type="number" placeholder="Trail amt" value={leg.trail_amount}
                onChange={(e) => onUpdate({ trail_amount: e.target.value })} className={numInput} />
              <PortalDropdown value={leg.trail_type} onChange={(v) => onUpdate({ trail_type: v as "points" | "percent" })}
                options={[{ value: "points", label: "Points" }, { value: "percent", label: "Percent" }]} />
            </div>
          )}
        </div>
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function AddLegNode({ data }: NodeProps) {
  const onAdd = data.onAdd as () => void;
  const disabled = data.disabled as boolean;
  return (
    <button onClick={onAdd} disabled={disabled}
      className="flex items-center gap-1.5 px-3 py-2 rounded-lg border-2 border-dashed text-xs font-semibold disabled:opacity-40"
      style={{ borderColor: C.orange, color: C.orange, background: "#fff7ed", width: 150 }}>
      <Plus size={14} /> Add leg
    </button>
  );
}

function ExitNode({ data }: NodeProps) {
  const takeProfitPct = data.takeProfitPct as string;
  const stopLossPct = data.stopLossPct as string;
  const exitTime = data.exitTime as string;
  const exitDaysBeforeExpiry = data.exitDaysBeforeExpiry as number;
  const onChange = data.onChange as (patch: Record<string, unknown>) => void;

  return (
    <div style={cardStyle}>
      <Handle type="target" position={Position.Left} />
      <div style={headerStyle("#ecfdf5", "#047857")}>Combined exit (all legs w/o their own)</div>
      <div className="p-2.5 space-y-2">
        <div className="grid grid-cols-2 gap-1.5">
          <div>
            <label style={fieldLabel}>Take profit %</label>
            <input type="number" value={takeProfitPct} onChange={(e) => onChange({ takeProfitPct: e.target.value })} className={numInput} />
          </div>
          <div>
            <label style={fieldLabel}>Stop loss %</label>
            <input type="number" value={stopLossPct} onChange={(e) => onChange({ stopLossPct: e.target.value })} className={numInput} />
          </div>
        </div>
        <div>
          <label style={fieldLabel}>Exit at time (optional)</label>
          <input type="time" value={exitTime} onChange={(e) => onChange({ exitTime: e.target.value })} className={numInput} />
        </div>
        <div>
          <label style={fieldLabel}>Exit N days before expiry</label>
          <input type="number" min={0} value={exitDaysBeforeExpiry} onChange={(e) => onChange({ exitDaysBeforeExpiry: parseInt(e.target.value) || 0 })} className={numInput} />
        </div>
      </div>
    </div>
  );
}

const nodeTypes = { symbol: SymbolNode, entry: EntryNode, leg: LegNode, addLeg: AddLegNode, exit: ExitNode };

// ---------------------------------------------------------------------------
// Canvas
// ---------------------------------------------------------------------------
interface StrategyFlowCanvasProps {
  symbols: string[];
  legs: LegForm[];
  onUpdateLeg: (idx: number, patch: Partial<LegForm>) => void;
  onRemoveLeg: (idx: number) => void;
  onAddLeg: () => void;
  entryMode: EntryMode;
  onEntryModeChange: (m: EntryMode) => void;
  entryTime: string;
  onEntryTimeChange: (v: string) => void;
  condition: ConditionForm;
  onConditionChange: (patch: Partial<ConditionForm>) => void;
  takeProfitPct: string;
  stopLossPct: string;
  exitTime: string;
  exitDaysBeforeExpiry: number;
  onExitChange: (patch: { takeProfitPct?: string; stopLossPct?: string; exitTime?: string; exitDaysBeforeExpiry?: number }) => void;
}

const LEG_ROW_HEIGHT = 420;

function defaultLayout(legCount: number): Record<string, { x: number; y: number }> {
  const positions: Record<string, { x: number; y: number }> = {
    symbol: { x: 0, y: legCount * LEG_ROW_HEIGHT / 2 - 60 },
    entry: { x: 300, y: legCount * LEG_ROW_HEIGHT / 2 - 90 },
    addLeg: { x: 600, y: legCount * LEG_ROW_HEIGHT },
    exit: { x: 940, y: legCount * LEG_ROW_HEIGHT / 2 - 100 },
  };
  for (let i = 0; i < legCount; i++) positions[`leg-${i}`] = { x: 600, y: i * LEG_ROW_HEIGHT };
  return positions;
}

function StrategyFlowCanvasInner(props: StrategyFlowCanvasProps) {
  const { symbols, legs, onUpdateLeg, onRemoveLeg, onAddLeg, entryMode, onEntryModeChange, entryTime,
    onEntryTimeChange, condition, onConditionChange, takeProfitPct, stopLossPct, exitTime,
    exitDaysBeforeExpiry, onExitChange } = props;

  const [dragPositions, setDragPositions] = useState<Record<string, { x: number; y: number }>>({});
  const hasCalendarSpread = legs.length > 1;

  const layout = useMemo(() => defaultLayout(legs.length), [legs.length]);

  const nodes: Node[] = useMemo(() => {
    const list: Node[] = [
      { id: "symbol", type: "symbol", position: dragPositions.symbol ?? layout.symbol, data: { symbols }, draggable: true },
      {
        id: "entry", type: "entry", position: dragPositions.entry ?? layout.entry,
        data: { entryMode, onEntryModeChange, entryTime, onEntryTimeChange, condition, onConditionChange },
        draggable: true,
      },
      ...legs.map((leg, idx): Node => ({
        id: `leg-${idx}`, type: "leg", position: dragPositions[`leg-${idx}`] ?? layout[`leg-${idx}`],
        data: {
          leg, idx, hasCalendarSpread,
          onUpdate: (patch: Partial<LegForm>) => onUpdateLeg(idx, patch),
          onRemove: legs.length > 1 ? () => onRemoveLeg(idx) : null,
        },
        draggable: true,
      })),
      {
        id: "addLeg", type: "addLeg", position: dragPositions.addLeg ?? layout.addLeg,
        data: { onAdd: onAddLeg, disabled: legs.length >= 8 }, draggable: true,
      },
      {
        id: "exit", type: "exit", position: dragPositions.exit ?? layout.exit,
        data: { takeProfitPct, stopLossPct, exitTime, exitDaysBeforeExpiry, onChange: onExitChange },
        draggable: true,
      },
    ];
    return list;
  }, [
    symbols, legs, entryMode, entryTime, condition, takeProfitPct, stopLossPct, exitTime, exitDaysBeforeExpiry,
    dragPositions, layout, hasCalendarSpread,
    onAddLeg, onConditionChange, onEntryModeChange, onEntryTimeChange, onExitChange, onRemoveLeg, onUpdateLeg,
  ]);

  const edges: Edge[] = useMemo(() => {
    const list: Edge[] = [{ id: "symbol-entry", source: "symbol", target: "entry" }];
    legs.forEach((_, idx) => {
      list.push({ id: `entry-leg-${idx}`, source: "entry", target: `leg-${idx}` });
      list.push({ id: `leg-${idx}-exit`, source: `leg-${idx}`, target: "exit" });
    });
    return list;
  }, [legs]);

  const onNodesChange: OnNodesChange = useCallback((changes: NodeChange[]) => {
    setDragPositions((prev) => {
      let next = prev;
      for (const c of changes) {
        if (c.type === "position" && c.position) {
          if (next === prev) next = { ...prev };
          next[c.id] = c.position;
        }
      }
      return next;
    });
  }, []);

  return (
    <div style={{ height: 560, background: "#fafafa", borderRadius: 12, border: `1px solid ${C.border2}` }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        nodeTypes={nodeTypes}
        fitView
        minZoom={0.3}
        maxZoom={1.5}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={16} size={1} color={C.border2} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

export default function StrategyFlowCanvas(props: StrategyFlowCanvasProps) {
  return (
    <ReactFlowProvider>
      <StrategyFlowCanvasInner {...props} />
    </ReactFlowProvider>
  );
}
