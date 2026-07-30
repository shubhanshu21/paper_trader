import { useState, useEffect, useRef } from "react";
import { Search, ChevronDown, ChevronUp, Briefcase, Settings, X, AlignLeft, TrendingUp, Trash2, MoreHorizontal, Plus, Check } from "lucide-react";
import { api, wsUrl, Instrument } from "../api";
import { C, FONT, inr } from "./Common";
import MarketDepthModal from "./MarketDepthModal";

interface DepthTarget {
  instrument_key: string;
  symbol: string;
  exchange: string;
}

interface LocalWatchlistItem {
  s: string;
  tag?: string;
  event?: boolean;
  hold?: number;
  chg: number;
  pct: number;
  ltp: number;
  // Fixed baseline (the instrument's reference price at watchlist load,
  // effectively the last close from Upstox's instrument master) that chg/pct
  // are computed against. Deliberately NOT "previous tick's ltp" — two
  // consecutive live ticks 2s apart very often carry the identical price
  // (no new trade in between), which made chg flicker to exactly 0 every
  // time that happened, and the render logic treats chg===0 as "no data,
  // render blank" — so prices appeared to randomly vanish and reappear.
  refPrice: number;
  hasQuote: boolean;
  instrument_key?: string;
  watchlist_id?: number;
  added_at?: string | null;
  order_index?: number | null;
}


interface WatchlistProps {
  onTrade: (order: { symbol: string; exch: string; side: "BUY" | "SELL"; instrument_key: string; last_price: number }) => void;
}

export default function Watchlist({ onTrade }: WatchlistProps) {
  const [hover, setHover] = useState<number | null>(null);
  const [page, setPageNumber] = useState(1);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<Instrument[]>([]);
  const [searching, setSearching] = useState(false);
  const [isFocused, setIsFocused] = useState(false);
  const [watchlist, setWatchlist] = useState<LocalWatchlistItem[]>([]);
  const [depthTarget, setDepthTarget] = useState<DepthTarget | null>(null);

  const searchContainerRef = useRef<HTMLDivElement>(null);

  // Load user watchlist from backend
  useEffect(() => {
    const loadWatchlist = async () => {
      try {
        const data = await api.getWatchlist(page);
        const transformed: LocalWatchlistItem[] = (data || []).map((item) => ({
          s: item.symbol,
          tag: item.instrument_type === 'INDEX' ? 'INDEX' : undefined,
          chg: 0,
          pct: 0,
          ltp: item.last_price || 0,
          refPrice: item.last_price || 0,
          hasQuote: false,
          instrument_key: item.instrument_key,
          watchlist_id: item.watchlist_id,
          added_at: item.added_at || undefined,
          order_index: item.order_index,
        }));
        setWatchlist(transformed);
      } catch (err) {
        console.error("Failed to load watchlist", err);
        setWatchlist([]);
      }
    };
    loadWatchlist();
  }, [page]);

  // Live LTPs via /ws/market — server polls the broker once per interval and
  // fans ticks out to every subscribed browser tab, so the client never polls.
  const instrumentKeysKey = watchlist
    .map(item => item.instrument_key)
    .filter((key): key is string => key !== undefined)
    .join(",");

  useEffect(() => {
    if (!instrumentKeysKey) return;

    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout>;
    let ws: WebSocket;

    const connect = () => {
      if (cancelled) return;
      ws = new WebSocket(wsUrl(`/ws/market?symbols=${encodeURIComponent(instrumentKeysKey)}`));

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "tick" && msg.instrument_key) {
            setWatchlist(prev => prev.map(item => {
              if (item.instrument_key !== msg.instrument_key) return item;
              const newLtp = msg.ltp;
              // refPrice is fixed (set once at load) — chg/pct measure "vs
              // reference," not "vs last tick," so an unchanged price
              // between two ticks doesn't get misread as chg===0/"no data".
              const ref = item.refPrice > 0 ? item.refPrice : newLtp;
              const chg = newLtp - ref;
              const pct = ref > 0 ? (chg / ref) * 100 : 0;
              return { ...item, ltp: newLtp, chg, pct, hasQuote: true };
            }));
          }
        } catch (err) {
          console.error("Failed to parse /ws/market message", err);
        }
      };
      ws.onclose = () => {
        if (!cancelled) reconnectTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      cancelled = true;
      clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, [instrumentKeysKey]);

  useEffect(() => {
    if (!searchQuery.trim()) {
      setSearchResults([]);
      return;
    }
    const delayDebounce = setTimeout(async () => {
      setSearching(true);
      try {
        const results = await api.searchInstruments(searchQuery);
        setSearchResults(results);
      } catch (err) {
        console.error("Search failed", err);
      } finally {
        setSearching(false);
      }
    }, 300);

    return () => clearTimeout(delayDebounce);
  }, [searchQuery]);

  // Live ticks for the search dropdown, same reference-price pattern as the
  // main watchlist above (see LocalWatchlistItem.refPrice comment) — keyed
  // by instrument_key since search results aren't part of `watchlist` state.
  const [searchQuotes, setSearchQuotes] = useState<Record<string, { ltp: number; refPrice: number }>>({});
  const searchKeysKey = searchResults.map(r => r.instrument_key).join(",");

  useEffect(() => {
    setSearchQuotes({});
    if (!searchKeysKey) return;

    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout>;
    let ws: WebSocket;

    const connect = () => {
      if (cancelled) return;
      ws = new WebSocket(wsUrl(`/ws/market?symbols=${encodeURIComponent(searchKeysKey)}`));
      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "tick" && msg.instrument_key) {
            setSearchQuotes(prev => {
              const existingRef = prev[msg.instrument_key]?.refPrice;
              const fallbackRef = searchResults.find(r => r.instrument_key === msg.instrument_key)?.last_price || 0;
              return {
                ...prev,
                [msg.instrument_key]: { ltp: msg.ltp, refPrice: existingRef ?? (fallbackRef > 0 ? fallbackRef : msg.ltp) },
              };
            });
          }
        } catch (err) {
          console.error("Failed to parse /ws/market message (search)", err);
        }
      };
      ws.onclose = () => {
        if (!cancelled) reconnectTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      cancelled = true;
      clearTimeout(reconnectTimer);
      ws?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchKeysKey]);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (searchContainerRef.current && !searchContainerRef.current.contains(event.target as Node)) {
        setIsFocused(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const handleWatchlistTrade = async (item: LocalWatchlistItem, side: "BUY" | "SELL") => {
    let key = item.instrument_key || "";
    let ltp = item.ltp;

    if (!key) {
      try {
        const results = await api.searchInstruments(item.s);
        const match = results.find(r => r.symbol.toUpperCase() === item.s.toUpperCase());
        if (match) {
          key = match.instrument_key;
          ltp = match.last_price || ltp;
        }
      } catch (e) {
        console.error("Watchlist trade lookup failed", e);
      }
    }

    onTrade({
      symbol: item.s,
      exch: item.tag || "NSE",
      side,
      instrument_key: key || item.s,
      last_price: ltp,
    });
  };

  const handleSearchResultTrade = (inst: Instrument, side: "BUY" | "SELL") => {
    onTrade({
      symbol: inst.symbol,
      exch: inst.exchange,
      side,
      instrument_key: inst.instrument_key,
      last_price: inst.last_price || 0,
    });
  };

  const handleAddToWatchlist = async (instrumentKey: string) => {
    try {
      await api.addToWatchlist(instrumentKey, page);
      const data = await api.getWatchlist(page);
      if (data) {
        const transformed: LocalWatchlistItem[] = data.map((item) => ({
          s: item.symbol,
          tag: item.instrument_type === 'INDEX' ? 'INDEX' : undefined,
          chg: 0,
          pct: 0,
          ltp: item.last_price || 0,
          refPrice: item.last_price || 0,
          hasQuote: false,
          instrument_key: item.instrument_key,
          watchlist_id: item.watchlist_id,
          added_at: item.added_at || undefined,
          order_index: item.order_index,
        }));
        setWatchlist(transformed);
      }
    } catch (err) {
      console.error("Failed to add to watchlist", err);
    }
  };

  const handleRemoveFromWatchlist = async (instrumentKey: string) => {
    try {
      await api.removeFromWatchlist(instrumentKey, page);
      setWatchlist(prev => prev.filter(item => item.instrument_key !== instrumentKey));
    } catch (err) {
      console.error("Failed to remove from watchlist", err);
    }
  };

  return (
    <aside
      ref={searchContainerRef}
      className="flex flex-col hidden w-[340px] shrink-0 lg:flex bg-white border-r relative"
      style={{ borderColor: C.border2, ...FONT }}
    >
      {/* Search Input */}
      <div
        className="flex items-center gap-3 px-4 shrink-0 bg-white border-b"
        style={{ height: 46, borderColor: C.border }}
      >
        <Search size={14} style={{ color: C.faint }} />
        <input
          placeholder="Search (infy bse, nifty fut, etc)"
          className="flex-1 text-[13px] bg-transparent outline-none py-1 text-gray-700"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          onFocus={() => setIsFocused(true)}
        />
        {searching && <span className="w-3 h-3 border-2 border-orange-500 border-t-transparent rounded-full animate-spin" />}
        {(searchQuery || isFocused) && (
          <button 
            onClick={() => {
              setSearchQuery("");
              setIsFocused(false);
            }} 
            className="text-gray-400 hover:text-gray-600 focus:outline-none"
          >
            <X size={14} />
          </button>
        )}
      </div>

      {/* Primary Watchlist Items Container */}
      <div className="flex-1 overflow-y-auto">
        {watchlist.map((w, i) => {
          const up = w.chg > 0;
          const col = !w.hasQuote ? C.muted : w.chg === 0 ? C.text : up ? C.green : C.red;
          const on = hover === i;
          return (
            <div
              key={i}
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
              className="relative flex items-center justify-between px-4 cursor-pointer transition-colors border-b"
              style={{
                height: 40,
                borderColor: C.border,
                background: on ? C.hover : "#fff",
              }}
            >
              <div className="flex items-baseline gap-1.5 truncate">
                <span style={{ color: col }} className="text-[13px] font-normal">{w.s}</span>
                {w.tag && (
                  <span className="text-[9px] tracking-wide text-gray-400 uppercase font-semibold">
                    {w.tag}
                  </span>
                )}
                {w.event && (
                  <span className="text-[8px] tracking-wide text-blue-500 font-bold bg-blue-50 px-1 rounded ml-0.5">
                    EVENT
                  </span>
                )}
              </div>

              {!on ? (
                <div className="flex items-center gap-2.5 tabular-nums text-[12px] font-normal">
                  {w.hold && w.hold > 0 ? (
                    <span className="flex items-center gap-0.5 text-[11px] text-gray-400 mr-1">
                      {w.hold}
                      <Briefcase size={10} />
                    </span>
                  ) : null}
                  <span className="w-12 text-right" style={{ color: col }}>
                    {w.hasQuote ? inr(w.chg) : ""}
                  </span>
                  <span className="w-12 text-right" style={{ color: col }}>
                    {w.hasQuote ? `${inr(w.pct)}%` : ""}
                  </span>
                  <span className="w-3 flex justify-center" style={{ color: col }}>
                    {!w.hasQuote || w.chg === 0 ? null : up ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                  </span>
                  <span className="w-16 text-right font-normal" style={{ color: col }}>
                    {w.ltp === 0 ? "" : inr(w.ltp)}
                  </span>
                </div>
              ) : (
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => handleWatchlistTrade(w, "BUY")}
                    className="w-[30px] py-0.5 text-[11px] font-bold text-white rounded focus:outline-none"
                    style={{ background: C.buyBg, color: C.buyText }}
                  >
                    B
                  </button>
                  <button
                    onClick={() => handleWatchlistTrade(w, "SELL")}
                    className="w-[30px] py-0.5 text-[11px] font-bold text-white rounded focus:outline-none"
                    style={{ background: C.sellBg, color: C.sellText }}
                  >
                    S
                  </button>
                  <button
                    onClick={() => w.instrument_key && setDepthTarget({ instrument_key: w.instrument_key, symbol: w.s, exchange: w.tag || "NSE" })}
                    className="p-1 rounded bg-gray-50 text-gray-400 hover:bg-gray-200 transition-colors focus:outline-none"
                    title="Market depth"
                  >
                    <AlignLeft size={12} />
                  </button>
                  <button className="p-1 rounded bg-gray-50 text-gray-400 hover:bg-gray-200 transition-colors focus:outline-none" title="Chart (coming soon)">
                    <TrendingUp size={12} />
                  </button>
                  <button
                    onClick={() => w.instrument_key && handleRemoveFromWatchlist(w.instrument_key)}
                    className="p-1 rounded bg-gray-50 text-gray-400 hover:bg-red-100 hover:text-red-600 transition-colors focus:outline-none"
                  >
                    <Trash2 size={12} />
                  </button>
                  <button className="p-1 rounded bg-gray-50 text-gray-400 hover:bg-gray-200 transition-colors focus:outline-none">
                    <MoreHorizontal size={12} />
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Focus Search Overlay Panel */}
      {isFocused && (
        <div 
          className="absolute left-0 right-0 bg-white z-20 flex flex-col border-b"
          style={{ top: 46, bottom: 42, borderColor: C.border2 }}
        >
          {/* Search results listing */}
          <div className="flex-1 overflow-y-auto divide-y" style={{ borderColor: C.border }}>
            {searchResults.length === 0 ? (
              <div className="p-4 text-center text-xs text-gray-400">
                {searchQuery ? "No matching instruments." : "Type to search standard equity/option/commodity master list."}
              </div>
            ) : (
              searchResults.map((inst) => {
                const isAdded = watchlist.some(item => item.instrument_key === inst.instrument_key);
                const isIndex = inst.instrument_type === "INDEX";
                const quote = searchQuotes[inst.instrument_key];
                const refPrice = quote?.refPrice ?? (inst.last_price || 0);
                const ltp = quote?.ltp ?? (inst.last_price || 0);
                const hasQuote = quote !== undefined || (inst.last_price || 0) > 0;
                const chg = refPrice > 0 ? ltp - refPrice : 0;
                const pct = refPrice > 0 ? (chg / refPrice) * 100 : 0;
                const up = chg > 0;
                const col = !hasQuote || isIndex ? C.muted : chg === 0 ? C.text : up ? C.green : C.red;
                return (
                  <div
                    key={inst.instrument_key}
                    className="group flex justify-between items-center px-4 py-2 hover:bg-gray-50 cursor-pointer transition-colors border-b text-xs h-[46px]"
                    style={{ borderColor: C.border }}
                  >
                    {/* Left: Symbol & Exchange */}
                    <div className="flex flex-col flex-1" onClick={() => handleSearchResultTrade(inst, "BUY")}>
                      <span className="font-semibold text-[13px] text-gray-800">
                        {inst.symbol}
                        {isIndex && <span className="ml-1.5 text-[9px] text-gray-400 font-bold uppercase tracking-wider">INDEX</span>}
                      </span>
                      {!isIndex && (
                        <span className="text-[9px] text-gray-400 font-bold uppercase tracking-wider">{inst.exchange.toLowerCase().replace('_', ' ')}</span>
                      )}
                    </div>
                    {/* Right: Default Price/Badge vs Hover Actions */}
                    <div className="flex items-center gap-3">
                      {/* Default View — chg / pct% / arrow / price, colored by direction */}
                      <div className="flex items-center gap-2 group-hover:hidden select-none tabular-nums">
                        {!isIndex && hasQuote && (
                          <>
                            <span className="w-14 text-right font-mono text-[12px]" style={{ color: col }}>{inr(chg)}</span>
                            <span className="w-14 text-right font-mono text-[12px]" style={{ color: col }}>{inr(pct)}%</span>
                            <span className="w-3 flex justify-center" style={{ color: col }}>
                              {chg === 0 ? null : up ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                            </span>
                          </>
                        )}
                        <span className="font-semibold font-mono text-[12px]" style={{ color: isIndex ? C.text : col }}>{inr(ltp)}</span>
                      </div>

                      {/* Hover Actions View */}
                      <div className="hidden group-hover:flex items-center gap-1.5 animate-fadeIn">
                        {/* Buy Button */}
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            handleSearchResultTrade(inst, "BUY");
                          }}
                          className="w-6 h-6 flex items-center justify-center text-[10px] font-bold text-white rounded transition-transform hover:scale-105"
                          style={{ backgroundColor: C.buyBg, color: C.buyText }}
                          title="Buy"
                        >
                          B
                        </button>

                        {/* Sell Button */}
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            handleSearchResultTrade(inst, "SELL");
                          }}
                          className="w-6 h-6 flex items-center justify-center text-[10px] font-bold text-white rounded transition-transform hover:scale-105"
                          style={{ backgroundColor: C.sellBg, color: C.sellText }}
                          title="Sell"
                        >
                          S
                        </button>

                        {/* Chart Icon */}
                        <button
                          className="p-1 rounded text-gray-400 hover:text-gray-700 hover:bg-gray-100 transition-colors"
                          title="Chart (coming soon)"
                        >
                          <TrendingUp size={14} />
                        </button>

                        {/* Depth Icon */}
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            setDepthTarget({ instrument_key: inst.instrument_key, symbol: inst.symbol, exchange: inst.exchange });
                          }}
                          className="p-1 rounded text-gray-400 hover:text-gray-700 hover:bg-gray-100 transition-colors"
                          title="Market Depth"
                        >
                          <AlignLeft size={14} />
                        </button>

                        {/* Add/Remove Pin */}
                        <button
                          onClick={async (e) => {
                            e.stopPropagation();
                            if (isAdded) {
                              await handleRemoveFromWatchlist(inst.instrument_key);
                            } else {
                              await handleAddToWatchlist(inst.instrument_key);
                            }
                          }}
                          className={`p-1 rounded transition-colors ${
                            isAdded 
                              ? "text-red-500 hover:bg-red-50" 
                              : "text-green-600 hover:bg-green-50"
                          }`}
                          title={isAdded ? "Remove from watchlist" : "Pin to watchlist"}
                        >
                          {isAdded ? <Check size={14} /> : <Plus size={14} />}
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}

      {/* Watchlist Footer / Pager */}
      <div
        className="flex items-center shrink-0 bg-white border-t"
        style={{ height: 42, borderColor: C.border2 }}
      >
        {[1, 2, 3, 4, 5, 6, 7].map((n) => (
          <button
            key={n}
            onClick={() => setPageNumber(n)}
            className="flex-1 h-full text-sm hover:bg-gray-50 transition-colors border-r"
            style={{
              color: page === n ? C.orange : C.muted,
              borderColor: C.border,
            }}
          >
            {n}
          </button>
        ))}
        <button className="flex items-center justify-center w-12 h-full hover:bg-gray-50">
          <Settings size={15} style={{ color: C.muted }} />
        </button>
      </div>

      {depthTarget && (
        <MarketDepthModal
          instrument={depthTarget}
          mode="paper"
          onClose={() => setDepthTarget(null)}
          onTrade={(side) => {
            const known = watchlist.find(w => w.instrument_key === depthTarget.instrument_key);
            onTrade({
              symbol: depthTarget.symbol,
              exch: depthTarget.exchange,
              side,
              instrument_key: depthTarget.instrument_key,
              last_price: known?.ltp || 0,
            });
            setDepthTarget(null);
          }}
        />
      )}
    </aside>
  );
}
