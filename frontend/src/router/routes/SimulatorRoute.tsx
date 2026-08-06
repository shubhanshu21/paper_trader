import { lazy, Suspense } from "react";
import RouteFallback from "../RouteFallback";

const SimulatorView = lazy(() => import("../../components/SimulatorView"));

export default function SimulatorRoute() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <SimulatorView />
    </Suspense>
  );
}
