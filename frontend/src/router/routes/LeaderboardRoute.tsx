import { lazy, Suspense } from "react";
import RouteFallback from "../RouteFallback";

const LeaderboardView = lazy(() => import("../../components/LeaderboardView"));

export default function LeaderboardRoute() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <LeaderboardView />
    </Suspense>
  );
}
