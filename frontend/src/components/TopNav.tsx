import { useState, useEffect, useRef } from "react";
import { useNavigate, useLocation, NavLink } from "react-router-dom";
import { User, LogOut, Menu, X } from "lucide-react";
import { User as UserType } from "../api";
import { C, FONT } from "../lib/format";
import NotificationBell from "./NotificationBell";
import MarketStatusBadge from "./MarketStatusBadge";
import UpstoxTokenBadge from "./UpstoxTokenBadge";

const NAV = ["Dashboard", "Strategies", "Orders", "Holdings", "Positions", "Leaderboard", "IV Screener", "OI Scanner", "Chain Replay"];

const navSlug = (navItem: string) => navItem.toLowerCase().replace(/\s+/g, "-");

interface TopNavProps {
  currentUser: UserType | null;
  onLogout: () => void;
}

export default function TopNav({ currentUser, onLogout }: TopNavProps) {
  const [menu, setMenu] = useState(false);
  const [mobileMenu, setMobileMenu] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const displayUser = currentUser || { username: "DEMOUSER", email: "demo@papertrade.com" };
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenu(false);
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);



  return (
    <header
      className="relative shrink-0 bg-white border-b flex items-center z-20"
      style={{ 
        height: 58, 
        borderColor: C.border2, 
        boxShadow: "0 2px 4px 0 rgba(0, 0, 0, 0.05)",
        ...FONT 
      }}
    >
      {/* Right Navigation Panel */}
      <div className="flex-1 flex items-center justify-between px-4 md:px-8 bg-white h-full">
        {/* Logo */}
        <div className="cursor-pointer flex items-center gap-2 select-none" onClick={() => navigate('/dashboard')}>
          <svg viewBox="0 0 100 100" className="w-7 h-7" style={{ fill: C.orange }}>
            <path d="M50,15 L85,50 L50,85 L15,50 Z" />
            <path d="M50,50 L85,85 L50,90 L15,85 Z" opacity="0.6" />
          </svg>
          <span className="text-[18px] font-semibold text-gray-800" style={{ letterSpacing: "-0.3px" }}>Paper</span>
        </div>

        {/* Desktop Navigation */}
        <nav className="hidden md:flex items-center gap-[28px] relative">
          {NAV.map((n) => {
            const slug = navSlug(n);
            const path = `/${slug}`;
            return (
              <NavLink
                key={n}
                to={path}
                className="text-[13px] font-normal transition-colors focus:outline-none"
                style={({ isActive }) => ({
                  color: isActive || (location.pathname === "/" && slug === "dashboard") ? C.orange : "#666666",
                  transition: "color 0.2s"
                })}
                onMouseEnter={(e) => {
                  const active = location.pathname === path || (location.pathname === "/" && slug === "dashboard");
                  if (!active) e.currentTarget.style.color = C.orange;
                }}
                onMouseLeave={(e) => {
                  const active = location.pathname === path || (location.pathname === "/" && slug === "dashboard");
                  if (!active) e.currentTarget.style.color = "#666666";
                }}
              >
                {n}
              </NavLink>
            );
          })}
          <div className="hidden md:block">
            <MarketStatusBadge />
          </div>
          <div className="hidden md:block">
            <UpstoxTokenBadge />
          </div>
          <div className="hidden md:block">
            <NotificationBell />
          </div>

          {/* Profile controls (Always visible, falls back to demo account if backend auth is disabled) */}
          <div className="relative" ref={menuRef}>
            <button className="flex items-center gap-2 focus:outline-none" onClick={() => setMenu((m) => !m)}>
              <span
                className="inline-block w-6 h-6 rounded-full text-white text-[11px] font-bold flex items-center justify-center shadow-inner"
                style={{ background: C.blue }}
              >
                {displayUser.username[0].toUpperCase()}
              </span>
              <span className="text-xs font-semibold text-gray-600 hover:text-orange-500 transition-colors uppercase tracking-wide">
                {displayUser.username}
              </span>
            </button>

            {menu && (
              <div
                className="absolute right-0 z-30 w-56 bg-white rounded shadow-lg border text-xs"
                style={{ top: 33, borderColor: C.border2 }}
              >
                {/* User Details Header */}
                <div className="px-5 py-3.5 border-b" style={{ borderColor: C.border }}>
                  <div className="text-[14px] font-semibold text-gray-800">
                    {displayUser.username.toLowerCase() === "demouser" ? "Demo User" : displayUser.username}
                  </div>
                  <div className="text-[11px] text-gray-400 mt-0.5 font-medium">
                    {displayUser.email || "demo@papertrade.com"}
                  </div>
                </div>

                {/* Menu Items */}
                <div className="py-1">
                  <button
                    onClick={() => { setMenu(false); navigate('/profile'); }}
                    className="flex items-center w-full gap-3 px-5 py-2.5 text-xs text-left text-gray-700 hover:bg-gray-50 transition-colors"
                  >
                    <User size={14} className="text-gray-400" />
                    <span>My profile <span className="text-gray-300">/ Settings</span></span>
                  </button>
                </div>

                <div className="border-t py-1" style={{ borderColor: C.border }}>
                  <button
                    onClick={() => { setMenu(false); onLogout(); }}
                    className="flex items-center w-full gap-3 px-5 py-2.5 text-xs text-left text-gray-700 hover:bg-gray-50 transition-colors"
                  >
                    <LogOut size={14} className="text-gray-400" />
                    <span>Logout</span>
                  </button>
                </div>
              </div>
            )}
          </div>
        </nav>

        {/* Mobile Menu Button */}
        <button
          className="md:hidden flex items-center gap-2 focus:outline-none"
          onClick={() => setMobileMenu(!mobileMenu)}
        >
          <span
            className="inline-block w-6 h-6 rounded-full text-white text-[11px] font-bold flex items-center justify-center shadow-inner"
            style={{ background: C.blue }}
          >
            {displayUser.username[0].toUpperCase()}
          </span>
          {mobileMenu ? <X size={20} /> : <Menu size={20} />}
        </button>
      </div>

      {/* Mobile Navigation Menu */}
      {mobileMenu && (
        <div className="md:hidden absolute top-16 left-0 right-0 bg-white border-b shadow-lg z-30" style={{ borderColor: C.border2 }}>
          <div className="flex flex-col p-4 space-y-3">
            <div className="flex items-center gap-2 flex-wrap"><MarketStatusBadge /><UpstoxTokenBadge /></div>
            {NAV.map((n) => {
              const slug = navSlug(n);
              const path = `/${slug}`;
              return (
                <NavLink
                  key={n}
                  to={path}
                  onClick={() => setMobileMenu(false)}
                  className="text-left text-sm font-medium py-2 px-3 rounded transition-colors"
                  style={({ isActive }) => {
                    const active = isActive || (location.pathname === "/" && slug === "dashboard");
                    return {
                      color: active ? C.orange : "#666666",
                      backgroundColor: active ? "#fff7ed" : "transparent"
                    };
                  }}
                >
                  {n}
                </NavLink>
              );
            })}
            <div className="border-t pt-3 mt-3" style={{ borderColor: C.border }}>
              <button
                onClick={() => { setMobileMenu(false); onLogout(); }}
                className="flex items-center gap-2 text-sm text-gray-700 py-2 px-3"
              >
                <LogOut size={16} />
                <span>Logout</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </header>
  );
}
