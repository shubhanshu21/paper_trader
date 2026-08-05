import { useState, useEffect, useCallback, useRef } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useDispatch, useSelector } from 'react-redux';
import { api, wsUrl } from './api';
import { loginSuccess, logout } from './store/slices/authSlice';
import { setLivePositions } from './store/slices/dataSlice';
import { RootState, AppDispatch } from './store';
import TopNav from './components/TopNav';
import Watchlist from './components/Watchlist';
import OrderWindow from './components/OrderWindow';
import { C } from './lib/format';

interface ActiveOrder {
  symbol: string;
  exch: string;
  side: 'BUY' | 'SELL';
  instrument_key: string;
  last_price: number;
}

export default function App() {
  const dispatch = useDispatch<AppDispatch>();
  const navigate = useNavigate();
  const location = useLocation();
  // Strategies has its own left column (the strategy list — see
  // StrategiesView.tsx) that takes over this same left-edge position once
  // the watchlist is out of the way, rather than a third column competing
  // for space with it.
  const showWatchlist = !location.pathname.startsWith('/strategies');
  const { isAuthenticated, currentUser } = useSelector((state: RootState) => state.auth);
  const [authEnabled, setAuthEnabled] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeOrder, setActiveOrder] = useState<ActiveOrder | null>(null);
  const [tradeError, setTradeError] = useState<string | null>(null);
  const positionsSocketRef = useRef<WebSocket | null>(null);

  const initAuth = useCallback(async () => {
    try {
      const health = await api.checkHealth();
      const isEnabled = health.auth === 'enabled';
      setAuthEnabled(isEnabled);

      if (isEnabled) {
        // Probe current user session
        try {
          const user = await api.me();
          dispatch(loginSuccess(user));
        } catch {
          // session cookie missing or expired — redirect immediately
          dispatch(logout());
          navigate('/login');
        }
      }
    } catch (err) {
      console.error('Failed to connect to backend api', err);
    } finally {
      setLoading(false);
    }
  }, [dispatch, navigate]);

  useEffect(() => {
    initAuth();
  }, [initAuth]);

  // Live positions feed (replaces one-shot REST fetch for Dashboard/Positions/
  // Holdings): server polls the broker every 3s and pushes MTM for both
  // options and equity positions over a single shared connection.
  const ready = !loading && (!authEnabled || isAuthenticated);
  useEffect(() => {
    if (!ready) return;

    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout>;

    const connect = () => {
      if (cancelled) return;
      const ws = new WebSocket(wsUrl('/ws/positions'));
      positionsSocketRef.current = ws;

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'positions') {
            dispatch(setLivePositions({ options: msg.options, equity: msg.equity }));
          }
        } catch (err) {
          console.error('Failed to parse /ws/positions message', err);
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
      positionsSocketRef.current?.close();
      positionsSocketRef.current = null;
    };
  }, [ready, dispatch]);

  const handleLogout = async () => {
    try {
      await api.logout();
    } catch (err) {
      console.error('Logout request failed', err);
    } finally {
      dispatch(logout());
      navigate('/login');
    }
  };

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen bg-white">
        <span className="w-8 h-8 border-3 border-orange-500 border-t-transparent rounded-full animate-spin mb-4" />
        <span className="text-sm font-semibold text-gray-500">Connecting to trading core...</span>
      </div>
    );
  }

  // If auth is enabled and we don't have a valid session, navigate to /login
  // explicitly rather than returning null — a blank screen while the router
  // sorts itself out is a worse UX than an immediate redirect.
  if (authEnabled && !isAuthenticated) {
    navigate('/login');
    return null;
  }

  // Render the main dashboard layout
  return (
    <div className="flex flex-col h-screen overflow-hidden bg-white" style={{ color: C.text }}>
      <TopNav currentUser={currentUser} onLogout={handleLogout} />
      <div className="flex flex-1 min-h-0">
        {showWatchlist && <Watchlist onTrade={setActiveOrder} />}
        <main className="flex-1 overflow-y-auto px-4 py-4 md:px-8 md:py-8 bg-white">
          <Outlet />
        </main>
      </div>
      {/* Trade error toast */}
      {tradeError && (
        <div
          className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-3 px-4 py-3 rounded-xl shadow-lg text-sm font-semibold text-white"
          style={{ backgroundColor: '#ef4444', maxWidth: 420 }}
        >
          <span className="flex-1">{tradeError}</span>
          <button
            onClick={() => setTradeError(null)}
            className="ml-2 text-white opacity-70 hover:opacity-100 focus:outline-none text-lg leading-none"
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      )}
      {activeOrder && (
        <OrderWindow
          order={activeOrder}
          onClose={() => { setActiveOrder(null); setTradeError(null); }}
          onPlaceOrder={async (qty, _price, product, mode, side) => {
            try {
              await api.executeManualTrade({
                instrument_key: activeOrder.instrument_key,
                direction: side,
                quantity: qty,
                product,
                mode,
              });
              setActiveOrder(null);
              setTradeError(null);
            } catch (err) {
              const msg = err instanceof Error ? err.message : 'Trade placement failed. Please try again.';
              setTradeError(msg);
            }
          }}
        />
      )}
    </div>
  );
}
