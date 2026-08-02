import { lazy, Suspense, useEffect } from "react";
import { useDispatch, useSelector } from "react-redux";
import { RootState, AppDispatch } from "../../store";
import { fetchOpenOptions, closeOptionPosition } from "../../store/thunks/dataThunks";
import RouteFallback from "../RouteFallback";

const PositionsView = lazy(() => import("../../components/PositionsView"));

export default function PositionsRoute() {
  const dispatch = useDispatch<AppDispatch>();
  const { openOptions, closingId } = useSelector((state: RootState) => state.data);

  useEffect(() => {
    dispatch(fetchOpenOptions());
  }, [dispatch]);

  const handleClosePosition = (id: number, type: "options" | "equity") => {
    if (type === "options") dispatch(closeOptionPosition(id));
  };

  return (
    <Suspense fallback={<RouteFallback />}>
      <PositionsView openOptions={openOptions} onClosePosition={handleClosePosition} closingId={closingId} />
    </Suspense>
  );
}
