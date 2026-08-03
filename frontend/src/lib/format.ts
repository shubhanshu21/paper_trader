// lib/format.ts — pure formatting constants/functions shared across components.
// Split out of components/Common.tsx (which must only export components for
// react-refresh/only-export-components) but still used internally by
// Common.tsx's TimePicker/DatePicker components, so those exports are kept
// here too rather than duplicated.

export const C = {
  orange: "#ff5722",
  blue: "#4184f3",
  green: "#4caf50",
  red: "#df514c",
  text: "#444444",
  muted: "#9b9b9b",
  faint: "#ababab",
  border: "#eeeeee",
  border2: "#e0e0e0",
  hover: "#fbfbfb",
  banner: "#fff9e6",
  bannerBorder: "#ffe082",
  buyBg: "#e2f2ff",
  sellBg: "#fff1f0",
  buyText: "#4184f3",
  sellText: "#ff5722",
  openStatusBg: "#f1f5f9",
  openStatusText: "#475569",
  rejectedStatusBg: "#fff1f0",
  rejectedStatusText: "#df514c",
  tableHeaderBg: "#fbfbfb",
  tableHeaderText: "#9b9b9b",
  tableBorder: "#eeeeee",
};

export const FONT = { fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif' };

export const inr = (n: number | null | undefined, d = 2) => {
  if (n === null || n === undefined) return "0.00";
  // Normalize -0 to 0 so it never renders as "-0.00"
  const v = n === 0 ? 0 : n;
  return v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
};

export const sign = (n: number | null | undefined) => {
  if (!n) return C.text;
  return n > 0 ? C.green : n < 0 ? C.red : C.text;
};

export const withSign = (n: number | null | undefined, d = 2) => {
  if (n === null || n === undefined) return "0.00";
  // Normalize -0 to 0 so sign prefix is never applied to a zero value
  const v = n === 0 ? 0 : n;
  return (v > 0 ? "+" : "") + inr(v, d);
};

export const inrWithSign = (n: number | null | undefined, d = 2) => {
  if (n === null || n === undefined) return "₹0.00";
  const v = n === 0 ? 0 : n;
  const absFormatted = Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
  return (v > 0 ? "+" : v < 0 ? "-" : "") + "₹" + absFormatted;
};

export const inrWithSignNoPlus = (n: number | null | undefined, d = 2) => {
  if (n === null || n === undefined) return "₹0.00";
  const v = n === 0 ? 0 : n;
  const absFormatted = Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
  return (v < 0 ? "-" : "") + "₹" + absFormatted;
};

export function to12Hour(value: string): { h: number; m: number; period: "AM" | "PM" } | null {
  if (!/^\d{2}:\d{2}$/.test(value)) return null;
  const [h24, m] = value.split(":").map(Number);
  const period: "AM" | "PM" = h24 >= 12 ? "PM" : "AM";
  const h = h24 % 12 === 0 ? 12 : h24 % 12;
  return { h, m, period };
}

/** "HH:MM" (24h) -> "hh:MM AM/PM", for displaying stored times anywhere outside the picker. Returns the input unchanged if unparseable. */
export function formatTime12h(value: string | null | undefined): string {
  if (!value) return "";
  const p = to12Hour(value);
  if (!p) return value;
  return `${String(p.h).padStart(2, "0")}:${String(p.m).padStart(2, "0")} ${p.period}`;
}

export const WEEKDAY_LABELS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];
export const MONTH_LABELS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

/** "YYYY-MM-DD" (or a full ISO datetime, date part used) -> "25 AUG 2026" — the one date format used everywhere in the app. */
export function fmtDate(iso: string): string {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return `${d} ${MONTH_LABELS[m - 1].slice(0, 3).toUpperCase()} ${y}`;
}

/** Full ISO datetime -> "25 AUG 2026, 10:00 AM" — fmtDate's date plus a 12h time, for timestamp fields (opened_at, run_at, created_at, ...). */
export function fmtDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const datePart = fmtDate(iso);
  let h = d.getHours();
  const m = d.getMinutes();
  const period = h >= 12 ? "PM" : "AM";
  h = h % 12 === 0 ? 12 : h % 12;
  return `${datePart}, ${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")} ${period}`;
}
