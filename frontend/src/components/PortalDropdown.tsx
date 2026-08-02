import { useState } from "react";
import {
  useFloating, useClick, useDismiss, useInteractions,
  FloatingPortal, offset, flip, shift, size, autoUpdate,
} from "@floating-ui/react";
import { ChevronDown, Check as CheckIcon } from "lucide-react";
import { C } from "./Common";

/**
 * PortalDropdown — a canvas-safe dropdown, used ONLY by in-canvas node
 * fields (StrategyFlowCanvas.tsx). Common.tsx's Select/TimePicker/
 * DatePicker render their open panel with `position: absolute`, anchored
 * to a `position: relative` parent — that breaks the moment the trigger
 * lives inside an @xyflow/react node, since panning/zooming the canvas
 * moves the node (via a CSS transform) without moving anything living
 * outside it, so the panel visually detaches from its trigger.
 *
 * Built on Floating UI (@floating-ui/react) rather than a hand-rolled
 * getBoundingClientRect() + fixed-position calculation — two rounds of
 * hand-rolled viewport-edge-flip logic still didn't reliably keep the
 * panel on-screen inside the canvas's stacked node layout. Deliberately
 * minimal: just useClick + useDismiss + positioning middleware — an
 * earlier version added useListNavigation/FloatingFocusManager for
 * keyboard nav, which added real complexity for a nice-to-have and
 * isn't worth the risk here; this app's other dropdowns (Common.tsx's
 * Select) don't have keyboard nav either, so this isn't a regression.
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
  const selected = options.find((o) => o.value === value);

  const { refs, floatingStyles, context } = useFloating({
    open,
    onOpenChange: setOpen,
    placement: "bottom-start",
    // autoUpdate keeps the panel glued to the trigger across scroll/resize
    // AND canvas pan/zoom (it watches the trigger element's actual
    // position via ResizeObserver/scroll listeners, not a one-shot
    // measurement) — this is exactly the part that was fragile by hand.
    whileElementsMounted: autoUpdate,
    middleware: [
      offset(4),
      flip({ padding: 8 }),
      shift({ padding: 8 }),
      size({
        padding: 8,
        apply({ availableHeight, elements }) {
          Object.assign(elements.floating.style, { maxHeight: `${Math.max(Math.min(availableHeight, 240), 80)}px` });
        },
      }),
    ],
  });

  const click = useClick(context);
  const dismiss = useDismiss(context);
  const { getReferenceProps, getFloatingProps, getItemProps } = useInteractions([click, dismiss]);

  return (
    <>
      <button
        ref={refs.setReference}
        type="button"
        disabled={disabled}
        className={`nodrag w-full flex items-center justify-between gap-1.5 px-2 py-1.5 border rounded text-xs bg-white transition-colors focus:outline-none disabled:bg-gray-100 disabled:text-gray-400 ${className}`}
        style={{ borderColor: open ? C.orange : C.border2 }}
        {...getReferenceProps()}
      >
        <span className="text-gray-800 font-medium truncate">{selected?.label ?? placeholder}</span>
        <ChevronDown size={12} className="shrink-0 transition-transform" style={{ color: C.muted, transform: open ? "rotate(180deg)" : undefined }} />
      </button>

      {open && (
        <FloatingPortal>
          <div
            ref={refs.setFloating}
            style={{ ...floatingStyles, borderColor: C.border2, width: "max-content", minWidth: 140 }}
            className="z-[9999] bg-white border rounded-lg shadow-lg overflow-y-auto py-1"
            {...getFloatingProps()}
          >
            {options.map((opt) => {
              const active = opt.value === value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  className="w-full flex items-center justify-between gap-2 px-3 py-1.5 text-xs text-left transition-colors focus:outline-none"
                  style={{ backgroundColor: active ? "#fff7ed" : "transparent" }}
                  onMouseEnter={(e) => { if (!active) e.currentTarget.style.backgroundColor = C.hover; }}
                  onMouseLeave={(e) => { if (!active) e.currentTarget.style.backgroundColor = "transparent"; }}
                  {...getItemProps({
                    // getItemProps() returns its OWN onClick — pass ours
                    // in here so Floating UI composes them, rather than a
                    // separate onClick prop a later spread would overwrite.
                    onClick: () => { onChange(opt.value); setOpen(false); },
                  })}
                >
                  <span>
                    <span className="font-medium" style={{ color: active ? C.orange : C.text }}>{opt.label}</span>
                    {opt.description && <span className="block text-[10px] text-gray-400 mt-0.5">{opt.description}</span>}
                  </span>
                  {active && <CheckIcon size={12} style={{ color: C.orange }} className="shrink-0" />}
                </button>
              );
            })}
          </div>
        </FloatingPortal>
      )}
    </>
  );
}
