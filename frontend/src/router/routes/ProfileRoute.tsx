import { lazy, Suspense, useEffect } from "react";
import { useDispatch, useSelector } from "react-redux";
import { RootState, AppDispatch } from "../../store";
import { fetchWalletSummary, fetchWalletLedger } from "../../store/thunks/dataThunks";
import RouteFallback from "../RouteFallback";

const ProfileView = lazy(() => import("../../components/ProfileView"));

export default function ProfileRoute() {
  const dispatch = useDispatch<AppDispatch>();
  const { currentUser } = useSelector((state: RootState) => state.auth);
  const { ledger } = useSelector((state: RootState) => state.data);

  useEffect(() => {
    dispatch(fetchWalletLedger());
  }, [dispatch]);

  const handleRefreshData = () => {
    dispatch(fetchWalletSummary());
    dispatch(fetchWalletLedger());
  };

  return (
    <Suspense fallback={<RouteFallback />}>
      <ProfileView currentUser={currentUser} ledger={ledger} onRefreshData={handleRefreshData} />
    </Suspense>
  );
}
