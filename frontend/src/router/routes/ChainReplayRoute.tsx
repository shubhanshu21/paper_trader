import { lazy, Suspense } from "react";
import RouteFallback from "../RouteFallback";

const ChainReplayView = lazy(() => import("../../components/ChainReplayView"));

export default function ChainReplayRoute() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <ChainReplayView />
    </Suspense>
  );
}
