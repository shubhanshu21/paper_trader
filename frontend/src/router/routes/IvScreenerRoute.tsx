import { lazy, Suspense } from "react";
import RouteFallback from "../RouteFallback";

const IvScreenerView = lazy(() => import("../../components/IvScreenerView"));

export default function IvScreenerRoute() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <IvScreenerView />
    </Suspense>
  );
}
