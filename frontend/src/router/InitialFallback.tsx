// router/InitialFallback.tsx — full-screen splash shown while the app
// shell's own authLoader runs (before ANY route, including its own lazy
// chunk, has loaded) — see index.tsx's HydrateFallback.
export default function InitialFallback() {
  return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-white">
      <span className="w-8 h-8 border-2 border-orange-500 border-t-transparent rounded-full animate-spin mb-4" style={{ borderWidth: '3px' }} />
      <span className="text-sm font-semibold text-gray-500 font-sans">Connecting to trading core...</span>
    </div>
  );
}
