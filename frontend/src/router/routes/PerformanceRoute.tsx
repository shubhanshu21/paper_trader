import { lazy, Suspense } from "react";
import RouteFallback from "../RouteFallback";

const PerformanceView = lazy(() => import("../../components/PerformanceView"));

export default function PerformanceRoute() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <PerformanceView />
    </Suspense>
  );
}
