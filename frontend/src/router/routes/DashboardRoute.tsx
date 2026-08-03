import { lazy, Suspense, useEffect } from "react";
import { useDispatch, useSelector } from "react-redux";
import { RootState, AppDispatch } from "../../store";
import { fetchWalletSummary, fetchOpenOptions, fetchOpenEquity } from "../../store/thunks/dataThunks";
import RouteFallback from "../RouteFallback";

// Own file (was inlined in router/index.tsx) so this view's code — and
// everything it imports — is only fetched when a user actually visits
// this route, instead of being bundled into the eagerly-loaded main
// chunk for every page. See StrategyBuilderModal.tsx's React.lazy() split
// for the same pattern applied to a single component.
const DashboardView = lazy(() => import("../../components/DashboardView"));

export default function DashboardRoute() {
  const dispatch = useDispatch<AppDispatch>();
  const { wallet, openEquity, openOptions, loading } = useSelector((state: RootState) => state.data);
  const { currentUser } = useSelector((state: RootState) => state.auth);

  useEffect(() => {
    dispatch(fetchWalletSummary());
    dispatch(fetchOpenOptions());
    dispatch(fetchOpenEquity());
  }, [dispatch]);

  return (
    <Suspense fallback={<RouteFallback />}>
      <DashboardView
        currentUser={currentUser}
        wallet={wallet}
        openEquity={openEquity}
        openOptions={openOptions}
        walletLoading={loading.wallet}
        setPage={() => {}}
      />
    </Suspense>
  );
}
