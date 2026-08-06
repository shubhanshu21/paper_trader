import { createBrowserRouter } from 'react-router-dom';
import App from '../App';
import Login from '../Login';
import { authLoader } from './authLoader';
import InitialFallback from './InitialFallback';
import DashboardRoute from './routes/DashboardRoute';
import StrategiesRoute from './routes/StrategiesRoute';
import OrdersRoute from './routes/OrdersRoute';
import HoldingsRoute from './routes/HoldingsRoute';
import PositionsRoute from './routes/PositionsRoute';
import ProfileRoute from './routes/ProfileRoute';
import LeaderboardRoute from './routes/LeaderboardRoute';
import IvScreenerRoute from './routes/IvScreenerRoute';
import OiScannerRoute from './routes/OiScannerRoute';
import ChainReplayRoute from './routes/ChainReplayRoute';

export const router = createBrowserRouter([
  {
    path: '/login',
    element: <Login />,
  },
  {
    path: '/',
    element: <App />,
    loader: authLoader,
    HydrateFallback: InitialFallback,
    children: [
      { index: true, element: <DashboardRoute /> },
      { path: 'dashboard', element: <DashboardRoute /> },
      { path: 'strategies', element: <StrategiesRoute /> },
      { path: 'orders', element: <OrdersRoute /> },
      { path: 'holdings', element: <HoldingsRoute /> },
      { path: 'positions', element: <PositionsRoute /> },
      { path: 'profile', element: <ProfileRoute /> },
      { path: 'leaderboard', element: <LeaderboardRoute /> },
      { path: 'iv-screener', element: <IvScreenerRoute /> },
      { path: 'oi-scanner', element: <OiScannerRoute /> },
      { path: 'chain-replay', element: <ChainReplayRoute /> },
    ],
  },
]);
