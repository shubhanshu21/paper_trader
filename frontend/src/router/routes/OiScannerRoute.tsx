import { lazy, Suspense } from "react";
import RouteFallback from "../RouteFallback";

const OiScannerView = lazy(() => import("../../components/OiScannerView"));

export default function OiScannerRoute() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <OiScannerView />
    </Suspense>
  );
}
