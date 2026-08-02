import { lazy, Suspense } from "react";
import { useDispatch, useSelector } from "react-redux";
import { RootState, AppDispatch } from "../../store";
import {
  fetchCustomStrategies, createCustomStrategy, updateCustomStrategy,
  updateCustomStrategyStatus, deleteCustomStrategy,
} from "../../store/thunks/customStrategiesThunks";
import RouteFallback from "../RouteFallback";

const StrategiesView = lazy(() => import("../../components/StrategiesView"));

export default function StrategiesRoute() {
  const dispatch = useDispatch<AppDispatch>();
  const { strategies, loading, statusUpdatingId, deletingId } = useSelector((state: RootState) => state.customStrategies);

  return (
    <Suspense fallback={<RouteFallback />}>
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
    </Suspense>
  );
}
