import { lazy, Suspense, useEffect } from "react";
import { useDispatch, useSelector } from "react-redux";
import { RootState, AppDispatch } from "../../store";
import { fetchOpenOptions, fetchOpenEquity, closeOptionPosition, closeEquityPosition, closeCustomPosition } from "../../store/thunks/dataThunks";
import RouteFallback from "../RouteFallback";

const PositionsView = lazy(() => import("../../components/PositionsView"));

export default function PositionsRoute() {
  const dispatch = useDispatch<AppDispatch>();
  const { openOptions, openEquity, ltps, closingId } = useSelector((state: RootState) => state.data);

  useEffect(() => {
    dispatch(fetchOpenOptions());
    dispatch(fetchOpenEquity());
  }, [dispatch]);

  const handleClosePosition = (id: number, type: "options" | "equity" | "custom") => {
    if (type === "options") dispatch(closeOptionPosition(id));
    else if (type === "equity") dispatch(closeEquityPosition(id));
    else if (type === "custom") dispatch(closeCustomPosition(id));
  };

  return (
    <Suspense fallback={<RouteFallback />}>
      <PositionsView
        openOptions={openOptions}
        openEquity={openEquity}
        ltps={ltps}
        onClosePosition={handleClosePosition}
        closingId={closingId}
      />
    </Suspense>
  );
}
