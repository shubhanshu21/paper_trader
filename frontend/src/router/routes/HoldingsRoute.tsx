import { lazy, Suspense, useEffect } from "react";
import { useDispatch, useSelector } from "react-redux";
import { RootState, AppDispatch } from "../../store";
import { fetchOpenEquity, closeEquityPosition } from "../../store/thunks/dataThunks";
import RouteFallback from "../RouteFallback";

const HoldingsView = lazy(() => import("../../components/HoldingsView"));

export default function HoldingsRoute() {
  const dispatch = useDispatch<AppDispatch>();
  const { openEquity, closedEquity, ltps, closingId } = useSelector((state: RootState) => state.data);

  useEffect(() => {
    dispatch(fetchOpenEquity());
  }, [dispatch]);

  const handleClosePosition = (id: number, type: "options" | "equity") => {
    if (type === "equity") dispatch(closeEquityPosition(id));
  };

  return (
    <Suspense fallback={<RouteFallback />}>
      <HoldingsView openEquity={openEquity} closedEquity={closedEquity} ltps={ltps} onClosePosition={handleClosePosition} closingId={closingId} />
    </Suspense>
  );
}
