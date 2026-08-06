import React, { useState, useCallback, useEffect, useRef } from "react";
import {
  AlertTriangle, LucideIcon, CheckCircle, AlertCircle, Info, X, Clock, ChevronDown, Check as CheckIcon,
  Calendar as CalendarIcon, ChevronsLeft, ChevronLeft, ChevronRight as ChevronRightIcon, ChevronsRight,
} from "lucide-react";
import { C, FONT, to12Hour, WEEKDAY_LABELS, MONTH_LABELS, fmtDate } from "../lib/format";
import { ToastContext, type ToastType, type Toast } from "../hooks/useToast";

export function Banner() {
  return (
    <div
      className="flex items-center gap-2.5 px-4 py-3 mb-6 text-[13px] rounded border"
      style={{ background: "#fff9e6", borderColor: "#ffe082", color: "#333" }}
    >
      <AlertTriangle size={15} style={{ color: "#ff9800" }} />
      <span>
        This is a simulated paper-trading platform — all balances, positions, and P&amp;L here are virtual, with no real money at risk.
      </span>
    </div>
  );
}

interface SectionTitleProps {
  icon: LucideIcon;
  children: React.ReactNode;
}

export function SectionTitle({ icon: Icon, children }: SectionTitleProps) {
  return (
    <div className="flex items-center gap-2 mb-5 font-medium" style={{ color: C.text }}>
      <Icon size={17} strokeWidth={1.6} style={{ color: C.muted }} />
      <span className="text-lg font-normal">{children}</span>
    </div>
  );
}

export function StatusPill({ status }: { status: string }) {
  const map: { [key: string]: { bg: string; fg: string } } = {
    OPEN: { bg: "#e2f2ff", fg: "#4184f3" },
    PENDING: { bg: "#e2f2ff", fg: "#4184f3" },
    REJECTED: { bg: "#fff1f0", fg: "#df514c" },
    CANCELLED: { bg: "#f5f5f5", fg: "#777777" },
    COMPLETE: { bg: "#ecfdf5", fg: "#10b981" },
    COMPLETED: { bg: "#ecfdf5", fg: "#10b981" },
    "TRIGGER PENDING": { bg: "#fff9e6", fg: "#ff9800" },
  };
  const st = map[status] || { bg: "#f5f5f5", fg: "#777777" };
  return (
    <span
      className="inline-block px-2 py-0.5 text-[10px] font-bold rounded uppercase tracking-wider"
      style={{ background: st.bg, color: st.fg }}
    >
      {status}
    </span>
  );
}

export function TypeTag({ t }: { t: string }) {
  const buy = t === "BUY";
  return (
    <span
      className="inline-block px-1.5 py-0.5 text-[10px] font-bold rounded"
      style={{
        background: buy ? C.buyBg : C.sellBg,
        color: buy ? C.buyText : C.sellText,
      }}
    >
      {t}
    </span>
  );
}

export function ColorBar({ height = 46 }) {
  const colors = [
    { c: "#4665f0", w: 18 },
    { c: "#22b8f0", w: 15 },
    { c: "#3c8ff0", w: 11 },
    { c: "#b13ec1", w: 12 },
    { c: "#6b46c1", w: 11 },
    { c: "#3b5bdb", w: 6 },
    { c: "#22c9d3", w: 6 },
    { c: "#0a9b6d", w: 5 },
    { c: "#7cc043", w: 4 },
    { c: "#cddc39", w: 4 },
  ];
  return (
    <div className="flex w-full overflow-hidden rounded" style={{ height }}>
      {colors.map((b, i) => (
        <div key={i} style={{ background: b.c, width: `${b.w}%` }} />
      ))}
    </div>
  );
}

interface TdProps {
  children?: React.ReactNode;
  right?: boolean;
  style?: React.CSSProperties;
  className?: string;
  colSpan?: number;
}

export const Td = ({ children, right, style, className, colSpan }: TdProps) => (
  <td
    colSpan={colSpan}
    className={`px-4 py-3 text-[13px] ${right ? "text-right tabular-nums" : "text-left"} ${className || ""}`}
    style={{ color: C.text, borderBottom: `1px solid ${C.border}`, fontSize: "13px", ...style }}
  >
    {children}
  </td>
);

interface ThProps {
  children?: React.ReactNode;
  right?: boolean;
  style?: React.CSSProperties;
  className?: string;
}

export const Th = ({ children, right, style, className }: ThProps) => (
  <th
    className={`px-4 py-3 text-[12px] font-medium ${right ? "text-right" : "text-left"} ${className || ""}`}
    style={{ color: C.tableHeaderText, borderBottom: `1px solid ${C.border2}`, background: C.tableHeaderBg, fontSize: "12px", fontWeight: 500, ...style }}
  >
    {children}
  </th>
);

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const removeToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const addToast = useCallback((message: string, type: ToastType = "info") => {
    const id = Math.random().toString(36).substring(2, 9);
    setToasts((prev) => [...prev, { id, message, type }]);
    setTimeout(() => removeToast(id), 4000);
  }, [removeToast]);

  const success = useCallback((msg: string) => addToast(msg, "success"), [addToast]);
  const error = useCallback((msg: string) => addToast(msg, "error"), [addToast]);
  const warning = useCallback((msg: string) => addToast(msg, "warning"), [addToast]);
  const info = useCallback((msg: string) => addToast(msg, "info"), [addToast]);

  return (
    <ToastContext.Provider value={{ toast: addToast, success, error, warning, info }}>
      {children}
      <div className="fixed top-4 right-4 z-50 flex flex-col gap-2 pointer-events-none max-w-sm w-full">
        {toasts.map((t) => {
          const iconMap = {
            success: <CheckCircle size={18} className="shrink-0" style={{ color: C.green }} />,
            error: <AlertCircle size={18} className="shrink-0" style={{ color: C.red }} />,
            warning: <AlertTriangle size={18} className="shrink-0" style={{ color: C.orange }} />,
            info: <Info size={18} className="shrink-0" style={{ color: C.blue }} />,
          };
          const bgMap = {
            success: "#ecfdf5",
            error: "#fff1f0",
            warning: "#fff9e6",
            info: "#e2f2ff",
          };
          const borderMap = {
            success: "#10b98130",
            error: "#df514c30",
            warning: "#ff980030",
            info: "#4184f330",
          };
          return (
            <div
              key={t.id}
              className="flex items-start gap-3 p-3.5 rounded-xl border shadow-lg pointer-events-auto transition-all duration-300"
              style={{
                backgroundColor: bgMap[t.type],
                borderColor: borderMap[t.type],
                fontFamily: FONT.fontFamily,
              }}
            >
              {iconMap[t.type]}
              <div className="flex-1 text-[13px] font-semibold text-gray-700 leading-tight">
                {t.message}
              </div>
              <button
                onClick={() => removeToast(t.id)}
                className="text-gray-400 hover:text-gray-600 focus:outline-none transition-colors"
              >
                <X size={14} />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

interface TimePickerProps {
  value: string;              // "HH:MM", 24-hour, or "" for unset
  onChange: (value: string) => void;
  placeholder?: string;
  allowClear?: boolean;
  className?: string;
}

const MINUTES = Array.from({ length: 60 }, (_, i) => i);
const HOURS_12 = Array.from({ length: 12 }, (_, i) => i + 1);


function to24Hour(h: number, m: number, period: "AM" | "PM"): string {
  let h24 = h % 12;
  if (period === "PM") h24 += 12;
  return `${String(h24).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

export function TimePicker({ value, onChange, placeholder = "Select time", allowClear = false, className = "" }: TimePickerProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const parsed = to12Hour(value);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const commit = (h: number, m: number, period: "AM" | "PM") => onChange(to24Hour(h, m, period));
  const h = parsed?.h ?? 9;
  const m = parsed?.m ?? 0;
  const period = parsed?.period ?? "AM";

  const colBtn = (active: boolean) =>
    `w-full px-3 py-1.5 text-sm text-center rounded-md transition-colors focus:outline-none ${
      active ? "font-bold text-white" : "text-gray-600 hover:bg-gray-100"
    }`;

  return (
    <div className={`relative ${className}`} ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between gap-2 px-3 py-2 border rounded-lg text-sm bg-white transition-colors focus:outline-none focus:ring-2"
        style={{ borderColor: open ? C.orange : C.border2 }}
      >
        <span className={parsed ? "text-gray-800 font-medium" : "text-gray-400"}>
          {parsed ? `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")} ${period}` : placeholder}
        </span>
        <span className="flex items-center gap-1.5 shrink-0">
          {allowClear && value && (
            <X size={13} className="text-gray-300 hover:text-gray-500" onClick={(e) => { e.stopPropagation(); onChange(""); }} />
          )}
          <Clock size={14} style={{ color: C.muted }} />
        </span>
      </button>

      {open && (
        <div
          className="absolute z-50 mt-1.5 bg-white border rounded-xl shadow-lg overflow-hidden"
          style={{ borderColor: C.border2, width: 190 }}
        >
          <div className="grid grid-cols-3 divide-x" style={{ borderColor: C.border }}>
            {[
              { items: HOURS_12, selected: h, onPick: (v: number) => commit(v, m, period) },
              { items: MINUTES, selected: m, onPick: (v: number) => commit(h, v, period) },
            ].map((col, ci) => (
              <div key={ci} className="h-40 overflow-y-auto py-1 px-1" style={{ scrollbarWidth: "thin" }}>
                {col.items.map((v) => (
                  <button
                    key={v}
                    type="button"
                    onClick={() => col.onPick(v)}
                    className={colBtn(v === col.selected)}
                    style={v === col.selected ? { backgroundColor: C.orange } : undefined}
                  >
                    {String(v).padStart(2, "0")}
                  </button>
                ))}
              </div>
            ))}
            <div className="py-1 px-1 flex flex-col justify-center gap-1">
              {(["AM", "PM"] as const).map((p) => (
                <button
                  key={p}
                  type="button"
                  onClick={() => commit(h, m, p)}
                  className={colBtn(p === period)}
                  style={p === period ? { backgroundColor: C.orange } : undefined}
                >
                  {p}
                </button>
              ))}
            </div>
          </div>
          <div className="border-t px-3 py-2 flex justify-end" style={{ borderColor: C.border }}>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-xs font-semibold px-3 py-1 rounded-md text-white transition-colors hover:opacity-90"
              style={{ backgroundColor: C.orange }}
            >
              Done
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

interface SelectOption {
  value: string;
  label: string;
  description?: string;
}

interface SelectProps {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  className?: string;
  disabled?: boolean;
}

export function Select({ value, onChange, options, className = "", disabled = false }: SelectProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const selected = options.find((o) => o.value === value);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  return (
    <div className={`relative ${className}`} ref={ref}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between gap-2 px-3 py-2 border rounded-lg text-sm bg-white transition-colors focus:outline-none disabled:bg-gray-100 disabled:text-gray-400 disabled:cursor-not-allowed"
        style={{ borderColor: open ? C.orange : C.border2 }}
      >
        <span className="text-gray-800 font-medium truncate">{selected?.label ?? "Select..."}</span>
        <ChevronDown size={14} className="shrink-0 transition-transform" style={{ color: C.muted, transform: open ? "rotate(180deg)" : undefined }} />
      </button>

      {open && (
        <div
          className="absolute z-50 mt-1.5 w-full bg-white border rounded-xl shadow-lg overflow-hidden py-1"
          style={{ borderColor: C.border2, maxHeight: 260, overflowY: "auto" }}
        >
          {options.map((opt) => {
            const active = opt.value === value;
            return (
              <button
                key={opt.value}
                type="button"
                onClick={() => { onChange(opt.value); setOpen(false); }}
                className="w-full flex items-center justify-between gap-2 px-3 py-2 text-sm text-left transition-colors focus:outline-none"
                style={{ backgroundColor: active ? "#fff7ed" : "transparent" }}
                onMouseEnter={(e) => { if (!active) e.currentTarget.style.backgroundColor = C.hover; }}
                onMouseLeave={(e) => { if (!active) e.currentTarget.style.backgroundColor = "transparent"; }}
              >
                <span>
                  <span className="font-medium" style={{ color: active ? C.orange : C.text }}>{opt.label}</span>
                  {opt.description && <span className="block text-xs text-gray-400 mt-0.5">{opt.description}</span>}
                </span>
                {active && <CheckIcon size={14} style={{ color: C.orange }} className="shrink-0" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

interface DatePickerProps {
  value: string;              // "YYYY-MM-DD", or "" for unset
  onChange: (value: string) => void;
  placeholder?: string;
  allowClear?: boolean;
  minDate?: string;
  maxDate?: string;
  className?: string;
}

export function DatePicker({ value, onChange, placeholder = "Select date", allowClear = false, minDate, maxDate, className = "" }: DatePickerProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const today = new Date();
  const parsed = value ? new Date(value + "T00:00:00") : null;
  const [viewYear, setViewYear] = useState((parsed ?? today).getFullYear());
  const [viewMonth, setViewMonth] = useState((parsed ?? today).getMonth()); // 0-indexed

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const openPicker = () => {
    const base = parsed ?? today;
    setViewYear(base.getFullYear());
    setViewMonth(base.getMonth());
    setOpen((o) => !o);
  };

  const shiftMonth = (delta: number) => {
    let y = viewYear, m = viewMonth + delta;
    if (m < 0) { m = 11; y -= 1; }
    if (m > 11) { m = 0; y += 1; }
    setViewYear(y); setViewMonth(m);
  };
  const shiftYear = (delta: number) => setViewYear((y) => y + delta);

  const toIso = (d: number) => `${viewYear}-${String(viewMonth + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
  const inRange = (iso: string) => (!minDate || iso >= minDate) && (!maxDate || iso <= maxDate);

  const firstWeekday = new Date(viewYear, viewMonth, 1).getDay();
  const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
  const todayIso = today.toISOString().slice(0, 10);

  return (
    <div className={`relative ${className}`} ref={ref}>
      <button
        type="button"
        onClick={openPicker}
        className="w-full flex items-center justify-between gap-2 px-3 py-2 border rounded-lg text-sm bg-white transition-colors focus:outline-none"
        style={{ borderColor: open ? C.orange : C.border2 }}
      >
        <span className={value ? "text-gray-800 font-medium" : "text-gray-400"}>{value ? fmtDate(value) : placeholder}</span>
        <span className="flex items-center gap-1.5 shrink-0">
          {allowClear && value && (
            <X size={13} className="text-gray-300 hover:text-gray-500" onClick={(e) => { e.stopPropagation(); onChange(""); }} />
          )}
          <CalendarIcon size={14} style={{ color: C.muted }} />
        </span>
      </button>

      {open && (
        <div className="absolute z-50 mt-1.5 bg-white border rounded-xl shadow-lg p-3" style={{ borderColor: C.border2, width: 260 }}>
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-0.5">
              <button type="button" onClick={() => shiftYear(-1)} className="p-1 rounded hover:bg-gray-100 focus:outline-none" title="Previous year" aria-label="Previous year">
                <ChevronsLeft size={14} style={{ color: C.muted }} />
              </button>
              <button type="button" onClick={() => shiftMonth(-1)} className="p-1 rounded hover:bg-gray-100 focus:outline-none" title="Previous month" aria-label="Previous month">
                <ChevronLeft size={14} style={{ color: C.muted }} />
              </button>
            </div>
            <span className="text-sm font-semibold text-gray-700">{MONTH_LABELS[viewMonth]} {viewYear}</span>
            <div className="flex items-center gap-0.5">
              <button type="button" onClick={() => shiftMonth(1)} className="p-1 rounded hover:bg-gray-100 focus:outline-none" title="Next month" aria-label="Next month">
                <ChevronRightIcon size={14} style={{ color: C.muted }} />
              </button>
              <button type="button" onClick={() => shiftYear(1)} className="p-1 rounded hover:bg-gray-100 focus:outline-none" title="Next year" aria-label="Next year">
                <ChevronsRight size={14} style={{ color: C.muted }} />
              </button>
            </div>
          </div>
          <div className="grid grid-cols-7 gap-0.5 mb-1">
            {WEEKDAY_LABELS.map((w) => (
              <div key={w} className="text-center text-[10px] font-semibold text-gray-400 py-1">{w}</div>
            ))}
          </div>
          <div className="grid grid-cols-7 gap-0.5">
            {Array.from({ length: firstWeekday }).map((_, i) => <div key={`b${i}`} />)}
            {Array.from({ length: daysInMonth }, (_, i) => i + 1).map((d) => {
              const iso = toIso(d);
              const disabled = !inRange(iso);
              const isSelected = iso === value;
              const isToday = iso === todayIso;
              return (
                <button
                  key={d}
                  type="button"
                  disabled={disabled}
                  onClick={() => { onChange(iso); setOpen(false); }}
                  className="aspect-square flex items-center justify-center text-xs rounded-md transition-colors focus:outline-none disabled:text-gray-300 disabled:cursor-not-allowed"
                  style={{
                    backgroundColor: isSelected ? C.orange : "transparent",
                    color: isSelected ? "#fff" : disabled ? undefined : isToday ? C.orange : C.text,
                    fontWeight: isSelected || isToday ? 700 : 400,
                    border: isToday && !isSelected ? `1px solid ${C.orange}` : "1px solid transparent",
                  }}
                  onMouseEnter={(e) => { if (!disabled && !isSelected) e.currentTarget.style.backgroundColor = C.hover; }}
                  onMouseLeave={(e) => { if (!disabled && !isSelected) e.currentTarget.style.backgroundColor = "transparent"; }}
                >
                  {d}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

