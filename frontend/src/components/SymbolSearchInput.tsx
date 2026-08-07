import { useEffect, useRef, useState } from "react";
import { Search } from "lucide-react";
import { api } from "../api";
import { C } from "../lib/format";

interface SymbolSearchInputProps {
  value: string;
  onChange: (symbol: string) => void;
  placeholder?: string;
  className?: string;
  // Restrict suggestions to one category — commodities only exist in the
  // live broker/instrument master, never in fno_bhavcopy, so a caller
  // backed by bhavcopy data should pass "NSE_FO" to hide commodity
  // suggestions that would 404 anyway.
  scope?: "ALL" | "NSE_FO";
}

type TradableSymbols = { stocks: string[]; indices: string[]; commodities: string[] };

let cachedSymbols: TradableSymbols | null = null;
let cachedSymbolsPromise: Promise<TradableSymbols | null> | null = null;

function loadSymbols() {
  if (cachedSymbols) return Promise.resolve(cachedSymbols);
  if (!cachedSymbolsPromise) {
    cachedSymbolsPromise = api.getTradableSymbols().then((data) => {
      cachedSymbols = data;
      return data;
    }).catch(() => {
      cachedSymbolsPromise = null; // allow a retry on next mount
      return null;
    });
  }
  return cachedSymbolsPromise;
}

export default function SymbolSearchInput({ value, onChange, placeholder = "Search symbol...", className = "", scope = "ALL" }: SymbolSearchInputProps) {
  const [query, setQuery] = useState(value);
  const [all, setAll] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    loadSymbols().then((data) => {
      if (!data) return;
      const list = scope === "NSE_FO" ? [...data.indices, ...data.stocks] : [...data.indices, ...data.stocks, ...data.commodities];
      setAll(list);
    });
  }, [scope]);

  useEffect(() => { setQuery(value); }, [value]);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const matches = query.trim()
    ? all.filter((s) => s.toLowerCase().includes(query.trim().toLowerCase())).slice(0, 12)
    : all.slice(0, 12);

  const pick = (symbol: string) => {
    onChange(symbol);
    setQuery(symbol);
    setOpen(false);
  };

  return (
    <div className={`relative ${className}`} ref={containerRef}>
      <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
      <input
        value={query}
        onChange={(e) => { setQuery(e.target.value.toUpperCase()); setOpen(true); }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && matches.length > 0) pick(matches[0]);
          else if (e.key === "Enter") pick(query.trim().toUpperCase());
          else if (e.key === "Escape") setOpen(false);
        }}
        placeholder={placeholder}
        className="w-full pl-9 pr-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-500"
        style={{ borderColor: C.border2 }}
      />
      {open && matches.length > 0 && (
        <div className="absolute z-50 w-full mt-1 bg-white border rounded-lg shadow-lg max-h-56 overflow-y-auto" style={{ borderColor: C.border2 }}>
          {matches.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => pick(s)}
              className="w-full px-4 py-2 text-left text-xs font-semibold text-gray-700 hover:bg-gray-100 transition-colors"
            >
              {s}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
