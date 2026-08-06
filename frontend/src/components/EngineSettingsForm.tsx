import { Plus, Trash2 } from "lucide-react";
import { C } from "../lib/format";
import { Select } from "./Common";
import { EngineSpec, FieldSpec, WEEKDAY_OPTIONS, getFieldValue, setFieldValue } from "../lib/engineSpecs";

interface BlackoutRange {
  start: string;
  end: string;
}

const inputClass = "w-full px-2.5 py-1.5 border rounded-lg text-xs font-semibold outline-none focus:border-orange-500";

function NumberField({ spec, rules, onChange }: { spec: FieldSpec; rules: Record<string, unknown>; onChange: (path: string, value: unknown) => void }) {
  const raw = getFieldValue(rules, spec.key);
  const value = typeof raw === "number" ? raw : "";
  return (
    <input
      type="number"
      className={inputClass}
      style={{ borderColor: C.border2 }}
      value={value}
      min={spec.min}
      max={spec.max}
      step={spec.step ?? "any"}
      onChange={(e) => onChange(spec.key, e.target.value === "" ? "" : Number(e.target.value))}
    />
  );
}

function TimeField({ spec, rules, onChange }: { spec: FieldSpec; rules: Record<string, unknown>; onChange: (path: string, value: unknown) => void }) {
  const raw = getFieldValue(rules, spec.key);
  const value = typeof raw === "string" ? raw : "";
  return (
    <input
      type="time"
      className={inputClass}
      style={{ borderColor: C.border2 }}
      value={value}
      onChange={(e) => onChange(spec.key, e.target.value)}
    />
  );
}

function SelectField({ spec, rules, onChange }: { spec: FieldSpec; rules: Record<string, unknown>; onChange: (path: string, value: unknown) => void }) {
  const raw = getFieldValue(rules, spec.key);
  const value = typeof raw === "string" ? raw : (spec.options?.[0]?.value ?? "");
  return <Select value={value} onChange={(v) => onChange(spec.key, v)} options={spec.options ?? []} />;
}

function GenericField({ spec, rules, onChange }: { spec: FieldSpec; rules: Record<string, unknown>; onChange: (path: string, value: unknown) => void }) {
  return (
    <div>
      <label className="block text-[11px] font-medium text-gray-500 mb-1">{spec.label}</label>
      {spec.type === "number" && <NumberField spec={spec} rules={rules} onChange={onChange} />}
      {spec.type === "time" && <TimeField spec={spec} rules={rules} onChange={onChange} />}
      {spec.type === "select" && <SelectField spec={spec} rules={rules} onChange={onChange} />}
      {spec.hint && <p className="text-[10px] text-gray-400 mt-1">{spec.hint}</p>}
    </div>
  );
}

function BlackoutDatesEditor({ rules, onChange }: { rules: Record<string, unknown>; onChange: (path: string, value: unknown) => void }) {
  const ranges = (Array.isArray(rules.blackout_dates) ? rules.blackout_dates : []) as BlackoutRange[];

  const update = (i: number, field: "start" | "end", value: string) => {
    const next = ranges.map((r, idx) => (idx === i ? { ...r, [field]: value } : r));
    onChange("blackout_dates", next);
  };
  const remove = (i: number) => onChange("blackout_dates", ranges.filter((_, idx) => idx !== i));
  const add = () => onChange("blackout_dates", [...ranges, { start: "", end: "" }]);

  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <label className="block text-[11px] font-medium text-gray-500">Blackout dates <span className="text-gray-400 font-normal">(no entries during these windows — earnings, events, etc.)</span></label>
        <button type="button" onClick={add} className="flex items-center gap-1 text-[11px] font-semibold" style={{ color: C.orange }}>
          <Plus size={12} /> Add window
        </button>
      </div>
      {ranges.length === 0 && <p className="text-xs text-gray-400">None — no blackout windows configured.</p>}
      <div className="space-y-2">
        {ranges.map((r, i) => (
          <div key={i} className="flex items-center gap-2">
            <input type="date" className={inputClass} style={{ borderColor: C.border2 }} value={r.start || ""} onChange={(e) => update(i, "start", e.target.value)} />
            <span className="text-xs text-gray-400">to</span>
            <input type="date" className={inputClass} style={{ borderColor: C.border2 }} value={r.end || ""} onChange={(e) => update(i, "end", e.target.value)} />
            <button type="button" onClick={() => remove(i)} className="p-1.5 rounded hover:bg-gray-100" style={{ color: C.red }}>
              <Trash2 size={13} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

function SessionScheduleEditor({ rules, onChange }: { rules: Record<string, unknown>; onChange: (path: string, value: unknown) => void }) {
  const symbolOptions = [{ value: "NIFTY", label: "NIFTY" }, { value: "SENSEX", label: "SENSEX" }];
  return (
    <div className="space-y-4">
      <div>
        <label className="block text-[11px] font-medium text-gray-500 mb-1.5">Which symbol trades which weekday</label>
        <div className="grid grid-cols-5 gap-2">
          {WEEKDAY_OPTIONS.slice(0, 5).map((wd) => (
            <div key={wd.value}>
              <div className="text-[10px] text-gray-400 mb-1">{wd.label}</div>
              <Select
                value={(getFieldValue(rules, `symbol_schedule.${wd.value}`) as string) || "NIFTY"}
                onChange={(v) => onChange(`symbol_schedule.${wd.value}`, v)}
                options={symbolOptions}
              />
            </div>
          ))}
        </div>
      </div>
      {["NIFTY", "SENSEX"].map((sym) => (
        <div key={sym}>
          <label className="block text-[11px] font-medium text-gray-500 mb-1.5">{sym} session clock</label>
          <div className="grid grid-cols-4 gap-2">
            {(["morning_entry", "morning_exit", "afternoon_entry", "afternoon_exit"] as const).map((slot) => (
              <div key={slot}>
                <div className="text-[10px] text-gray-400 mb-1">{slot.replace("_", " ")}</div>
                <input
                  type="time"
                  className={inputClass}
                  style={{ borderColor: C.border2 }}
                  value={(getFieldValue(rules, `sessions.${sym}.${slot}`) as string) || ""}
                  onChange={(e) => onChange(`sessions.${sym}.${slot}`, e.target.value)}
                />
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

export default function EngineSettingsForm({
  spec,
  rules,
  onChange,
}: {
  spec: EngineSpec;
  rules: Record<string, unknown>;
  onChange: (rules: Record<string, unknown>) => void;
}) {
  const setPath = (path: string, value: unknown) => onChange(setFieldValue(rules, path, value));

  return (
    <div className="space-y-5">
      <div className="rounded-xl border p-3.5" style={{ borderColor: C.border2, background: C.hover }}>
        <p className="text-xs text-gray-600">{spec.tagline}</p>
        {spec.fixedSymbols && (
          <p className="text-[11px] text-gray-500 mt-1.5">Always trades <strong>{spec.fixedSymbols.join(" + ")}</strong> — the symbol picker above is ignored for this engine.</p>
        )}
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-4">
        {spec.fields.map((f) => (
          <GenericField key={f.key} spec={f} rules={rules} onChange={setPath} />
        ))}
      </div>

      {spec.needsSessionSchedule && <SessionScheduleEditor rules={rules} onChange={setPath} />}
      {spec.needsBlackoutDates && <BlackoutDatesEditor rules={rules} onChange={setPath} />}
    </div>
  );
}
