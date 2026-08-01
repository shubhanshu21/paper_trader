import { createBrowserRouter, redirect } from 'react-router-dom';
import { useDispatch, useSelector } from 'react-redux';
import App from '../App';
import Login from '../Login';
import DashboardView from '../components/DashboardView';
import StrategiesView from '../components/StrategiesView';
import OrdersView from '../components/OrdersView';
import HoldingsView from '../components/HoldingsView';
import PositionsView from '../components/PositionsView';
import ProfileView from '../components/ProfileView';
import LeaderboardView from '../components/LeaderboardView';
import AdvancedOrdersView from '../components/AdvancedOrdersView';
import { api } from '../api';
import { fetchWalletSummary, fetchOrders, fetchOpenOptions, fetchOpenEquity, fetchWalletLedger, closeOptionPosition, closeEquityPosition } from '../store/thunks/dataThunks';
import {
  fetchAdvancedOrders, createOcoOrder, cancelOcoOrder,
  createTrailingStop, cancelTrailingStop, createBracketOrder, cancelBracketOrder,
} from '../store/thunks/advancedOrdersThunks';
import {
  fetchCustomStrategies, createCustomStrategy, updateCustomStrategy,
  updateCustomStrategyStatus, deleteCustomStrategy,
} from '../store/thunks/customStrategiesThunks';
import { RootState, AppDispatch } from '../store';

// Connected components that use Redux
const DashboardConnected = () => {
  const { wallet, openEquity, openOptions } = useSelector((state: RootState) => state.data);
  return <DashboardView wallet={wallet} openEquity={openEquity} openOptions={openOptions} setPage={() => {}} />;
};

const OrdersConnected = () => {
  const { orders } = useSelector((state: RootState) => state.data);
  return <OrdersView orders={orders} />;
};

const HoldingsConnected = () => {
  const dispatch = useDispatch<AppDispatch>();
  const { openEquity, closedEquity, ltps } = useSelector((state: RootState) => state.data);
  const { closingId } = useSelector((state: RootState) => state.data);
  
  const handleClosePosition = (id: number, type: 'options' | 'equity') => {
    if (type === 'equity') {
      dispatch(closeEquityPosition(id));
    }
  };
  
  return <HoldingsView openEquity={openEquity} closedEquity={closedEquity} ltps={ltps} onClosePosition={handleClosePosition} closingId={closingId} />;
};

const PositionsConnected = () => {
  const dispatch = useDispatch<AppDispatch>();
  const { openOptions } = useSelector((state: RootState) => state.data);
  const { closingId } = useSelector((state: RootState) => state.data);
  
  const handleClosePosition = (id: number, type: 'options' | 'equity') => {
    if (type === 'options') {
      dispatch(closeOptionPosition(id));
    }
  };
  
  return <PositionsView openOptions={openOptions} onClosePosition={handleClosePosition} closingId={closingId} />;
};

const AdvancedOrdersConnected = () => {
  const dispatch = useDispatch<AppDispatch>();
  const { ocoOrders, trailingStops, bracketOrders, loading, creating, cancellingId } = useSelector((state: RootState) => state.advancedOrders);

  return (
    <AdvancedOrdersView
      ocoOrders={ocoOrders}
      trailingStops={trailingStops}
      bracketOrders={bracketOrders}
      loading={loading}
      creating={creating}
      cancellingId={cancellingId}
      onRefresh={() => dispatch(fetchAdvancedOrders())}
      onCreateOco={(req) => dispatch(createOcoOrder(req)).unwrap()}
      onCreateTrailingStop={(req) => dispatch(createTrailingStop(req)).unwrap()}
      onCreateBracket={(req) => dispatch(createBracketOrder(req)).unwrap()}
      onCancelOco={(id) => dispatch(cancelOcoOrder(id))}
      onCancelTrailingStop={(id) => dispatch(cancelTrailingStop(id))}
      onCancelBracket={(id) => dispatch(cancelBracketOrder(id))}
    />
  );
};

const StrategiesConnected = () => {
  const dispatch = useDispatch<AppDispatch>();
  const { strategies, loading, statusUpdatingId, deletingId } = useSelector((state: RootState) => state.customStrategies);

  return (
    <StrategiesView
      strategies={strategies}
      loading={loading}
      statusUpdatingId={statusUpdatingId}
      deletingId={deletingId}
      onRefresh={() => dispatch(fetchCustomStrategies())}
      onCreate={(payload) => dispatch(createCustomStrategy(payload)).unwrap()}
      onUpdate={(id, payload) => dispatch(updateCustomStrategy({ id, payload })).unwrap()}
      onStatusChange={(id, status) => dispatch(updateCustomStrategyStatus({ id, status })).unwrap()}
      onDelete={(id) => dispatch(deleteCustomStrategy(id)).unwrap()}
    />
  );
};

const ProfileConnected = () => {
  const dispatch = useDispatch<AppDispatch>();
  const { currentUser } = useSelector((state: RootState) => state.auth);
  const { ledger } = useSelector((state: RootState) => state.data);

  const handleRefreshData = () => {
    dispatch(fetchWalletSummary());
    dispatch(fetchWalletLedger());
  };

  return <ProfileView currentUser={currentUser} ledger={ledger} onRefreshData={handleRefreshData} />;
};

const ProfileLoader = () => {
  const dispatch = useDispatch<AppDispatch>();
  dispatch(fetchWalletLedger());
  return null;
};

// Data loader components that fetch data on mount
const DashboardLoader = () => {
  const dispatch = useDispatch<AppDispatch>();
  dispatch(fetchWalletSummary());
  dispatch(fetchOpenOptions());
  dispatch(fetchOpenEquity());
  return null;
};

const OrdersLoader = () => {
  const dispatch = useDispatch<AppDispatch>();
  dispatch(fetchOrders());
  return null;
};

const HoldingsLoader = () => {
  const dispatch = useDispatch<AppDispatch>();
  dispatch(fetchOpenEquity());
  return null;
};

const PositionsLoader = () => {
  const dispatch = useDispatch<AppDispatch>();
  dispatch(fetchOpenOptions());
  return null;
};

const loader = async () => {
  try {
    const health = await api.checkHealth();
    const isEnabled = health.auth === 'enabled';
    
    if (isEnabled) {
      try {
        await api.me();
        return null;
      } catch {
        return redirect('/login');
      }
    }
    return null;
  } catch {
    return null;
  };
};

const InitialFallback = () => (
  <div className="flex flex-col items-center justify-center min-h-screen bg-white">
    <span className="w-8 h-8 border-2 border-orange-500 border-t-transparent rounded-full animate-spin mb-4" style={{ borderWidth: '3px' }} />
    <span className="text-sm font-semibold text-gray-500 font-sans">Connecting to trading core...</span>
  </div>
);

export const router = createBrowserRouter([
  {
    path: '/login',
    element: <Login />,
  },
  {
    path: '/',
    element: <App />,
    loader,
    HydrateFallback: InitialFallback,
    children: [
      {
        index: true,
        element: (
          <>
            <DashboardLoader />
            <DashboardConnected />
          </>
        ),
      },
      {
        path: 'dashboard',
        element: (
          <>
            <DashboardLoader />
            <DashboardConnected />
          </>
        ),
      },
      {
        path: 'strategies',
        element: <StrategiesConnected />,
      },
      {
        path: 'orders',
        element: (
          <>
            <OrdersLoader />
            <OrdersConnected />
          </>
        ),
      },
      {
        path: 'holdings',
        element: (
          <>
            <HoldingsLoader />
            <HoldingsConnected />
          </>
        ),
      },
      {
        path: 'positions',
        element: (
          <>
            <PositionsLoader />
            <PositionsConnected />
          </>
        ),
      },
      {
        path: 'profile',
        element: (
          <>
            <ProfileLoader />
            <ProfileConnected />
          </>
        ),
      },
      {
        path: 'leaderboard',
        element: <LeaderboardView />,
      },
      {
        path: 'advanced-orders',
        element: <AdvancedOrdersConnected />,
      },
    ],
  },
]);
