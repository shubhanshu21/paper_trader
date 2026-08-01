import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ChevronDown, Check as CheckIcon } from "lucide-react";
import { C } from "./Common";

/**
 * PortalDropdown — a canvas-safe dropdown, used ONLY by in-canvas node
 * fields (StrategyFlowCanvas.tsx). Common.tsx's Select/TimePicker/
 * DatePicker render their open panel with `position: absolute`, anchored
 * to a `position: relative` parent — that breaks the moment the trigger
 * lives inside an @xyflow/react node, since panning/zooming the canvas
 * moves the node (via a CSS transform) without moving anything living
 * outside it, so the panel visually detaches from its trigger. This
 * renders the panel via `createPortal` to `document.body` instead, with
 * its position computed directly from the trigger's own
 * `getBoundingClientRect()` (viewport coordinates, unaffected by the
 * canvas's transform) and recomputed on every open/scroll/resize.
 *
 * Every existing Select/TimePicker/DatePicker usage elsewhere in the app
 * is untouched — this is a separate component, not a shared refactor.
 */
interface PortalDropdownOption {
  value: string;
  label: string;
  description?: string;
}

interface PortalDropdownProps {
  value: string;
  onChange: (value: string) => void;
  options: PortalDropdownOption[];
  placeholder?: string;
  disabled?: boolean;
  className?: string;
}

export function PortalDropdown({ value, onChange, options, placeholder = "Select...", disabled = false, className = "" }: PortalDropdownProps) {
  const [open, setOpen] = useState(false);
  const [rect, setRect] = useState<{ top: number; left: number; width: number } | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const selected = options.find((o) => o.value === value);

  const reposition = () => {
    const el = triggerRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setRect({ top: r.bottom + 4, left: r.left, width: r.width });
  };

  useEffect(() => {
    if (!open) return;
    reposition();
    const close = (e: MouseEvent) => {
      const target = e.target as Node;
      if (triggerRef.current?.contains(target) || panelRef.current?.contains(target)) return;
      setOpen(false);
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    return () => {
      window.removeEventListener("mousedown", close);
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition);
    };
  }, [open]);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        className={`w-full flex items-center justify-between gap-1.5 px-2 py-1.5 border rounded text-xs bg-white transition-colors focus:outline-none disabled:bg-gray-100 disabled:text-gray-400 ${className}`}
        style={{ borderColor: open ? C.orange : C.border2 }}
      >
        <span className="text-gray-800 font-medium truncate">{selected?.label ?? placeholder}</span>
        <ChevronDown size={12} className="shrink-0 transition-transform" style={{ color: C.muted, transform: open ? "rotate(180deg)" : undefined }} />
      </button>

      {open && rect && createPortal(
        <div
          ref={panelRef}
          className="fixed z-[9999] bg-white border rounded-lg shadow-lg overflow-hidden py-1"
          style={{ borderColor: C.border2, top: rect.top, left: rect.left, width: Math.max(rect.width, 140), maxHeight: 240, overflowY: "auto" }}
        >
          {options.map((opt) => {
            const active = opt.value === value;
            return (
              <button
                key={opt.value}
                type="button"
                onClick={() => { onChange(opt.value); setOpen(false); }}
                className="w-full flex items-center justify-between gap-2 px-3 py-1.5 text-xs text-left transition-colors focus:outline-none"
                style={{ backgroundColor: active ? "#fff7ed" : "transparent" }}
                onMouseEnter={(e) => { if (!active) e.currentTarget.style.backgroundColor = C.hover; }}
                onMouseLeave={(e) => { if (!active) e.currentTarget.style.backgroundColor = "transparent"; }}
              >
                <span>
                  <span className="font-medium" style={{ color: active ? C.orange : C.text }}>{opt.label}</span>
                  {opt.description && <span className="block text-[10px] text-gray-400 mt-0.5">{opt.description}</span>}
                </span>
                {active && <CheckIcon size={12} style={{ color: C.orange }} className="shrink-0" />}
              </button>
            );
          })}
        </div>,
        document.body
      )}
    </>
  );
}
