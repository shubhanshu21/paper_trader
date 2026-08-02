import { lazy, Suspense, useEffect } from "react";
import { useDispatch, useSelector } from "react-redux";
import { RootState, AppDispatch } from "../../store";
import { fetchOrders } from "../../store/thunks/dataThunks";
import RouteFallback from "../RouteFallback";

const OrdersView = lazy(() => import("../../components/OrdersView"));

export default function OrdersRoute() {
  const dispatch = useDispatch<AppDispatch>();
  const { orders } = useSelector((state: RootState) => state.data);

  useEffect(() => {
    dispatch(fetchOrders());
  }, [dispatch]);

  return (
    <Suspense fallback={<RouteFallback />}>
      <OrdersView orders={orders} />
    </Suspense>
  );
}
