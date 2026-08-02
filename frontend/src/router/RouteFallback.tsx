// router/RouteFallback.tsx — shared Suspense fallback for lazy-loaded
// route views (see router/routes/*.tsx) — same spinner style as
// index.tsx's own InitialFallback (app-shell-level), just sized for an
// in-page swap rather than a full-screen initial load.
export default function RouteFallback() {
  return (
    <div className="flex items-center justify-center py-24">
      <span className="w-6 h-6 border-2 border-orange-500 border-t-transparent rounded-full animate-spin" style={{ borderWidth: "3px" }} />
    </div>
  );
}
