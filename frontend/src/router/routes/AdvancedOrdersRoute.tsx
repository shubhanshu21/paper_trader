import { lazy, Suspense } from "react";
import { useDispatch, useSelector } from "react-redux";
import { RootState, AppDispatch } from "../../store";
import {
  fetchAdvancedOrders, createOcoOrder, cancelOcoOrder,
  createTrailingStop, cancelTrailingStop, createBracketOrder, cancelBracketOrder,
} from "../../store/thunks/advancedOrdersThunks";
import RouteFallback from "../RouteFallback";

const AdvancedOrdersView = lazy(() => import("../../components/AdvancedOrdersView"));

export default function AdvancedOrdersRoute() {
  const dispatch = useDispatch<AppDispatch>();
  const { ocoOrders, trailingStops, bracketOrders, loading, creating, cancellingId } = useSelector((state: RootState) => state.advancedOrders);

  return (
    <Suspense fallback={<RouteFallback />}>
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
    </Suspense>
  );
}
