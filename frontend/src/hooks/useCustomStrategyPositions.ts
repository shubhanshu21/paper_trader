import { useEffect, useState } from "react";
import { wsUrl } from "../api";

export interface CustomPositionRow {
  id: number;
  strategy_id: number;
  strategy_name: string;
  symbol: string;
  mode: string;
  option_type: string | null;
  strike: number | null;
  expiry: string | null;
  expiry_mode: "WEEKLY" | "MONTHLY";
  instrument_type: string;
  transaction_type: "BUY" | "SELL";
  quantity: number;
  entry_price: number;
  ltp: number | null;
  pnl: number | null;
  chg_pct: number | null;
  opened_at: string | null;
}

/**
 * Live (WebSocket-pushed, never polled) open custom-strategy legs for the
 * logged-in user, across ALL of their strategies — server pushes on its
 * own timer (see ws_custom_strategy_positions.py), the client only ever
 * receives. Shared by PositionsView.tsx (all strategies) and
 * StrategiesView.tsx (filtered to the selected strategy's open legs).
 */
export function useCustomStrategyPositions(): CustomPositionRow[] {
  const [rows, setRows] = useState<CustomPositionRow[]>([]);

  useEffect(() => {
    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout>;

    const connect = () => {
      const ws = new WebSocket(wsUrl("/ws/custom-strategy-positions"));
      ws.onmessage = (event) => {
        if (cancelled) return;
        const msg = JSON.parse(event.data);
        if (msg.type === "positions") setRows(msg.rows);
      };
      ws.onclose = () => {
        if (!cancelled) reconnectTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
      return ws;
    };

    const ws = connect();
    return () => { cancelled = true; clearTimeout(reconnectTimer); ws.close(); };
  }, []);

  return rows;
}
