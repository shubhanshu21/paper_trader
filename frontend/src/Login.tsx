import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useDispatch } from "react-redux";
import { Eye, EyeOff, Play, Apple } from "lucide-react";
import { api } from "./api";
import { loginSuccess } from "./store/slices/authSlice";
import { C, FONT } from "./components/Common";

export default function Login() {
  const navigate = useNavigate();
  const dispatch = useDispatch();
  const [isRegister, setIsRegister] = useState(false);
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      if (isRegister) {
        if (!email) {
          setError("Email is required.");
          setLoading(false);
          return;
        }
        await api.register(username, email, password);
        const loginRes = await api.login(username, password);
        dispatch(loginSuccess(loginRes.user));
        navigate('/dashboard');
      } else {
        const res = await api.login(username, password);
        dispatch(loginSuccess(res.user));
        navigate('/dashboard');
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : undefined;
      setError(message || "Authentication failed. Please check your credentials.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div 
      className="flex flex-col items-center justify-center min-h-screen bg-slate-50/30 px-4 py-12 select-none"
      style={FONT}
    >
      {/* Login Card */}
      <div 
        className="w-full max-w-[390px] bg-white rounded border p-10 shadow-xs"
        style={{ borderColor: C.border2 }}
      >
        {/* Brand Logo */}
        <div className="flex items-center justify-center gap-2 mb-6 select-none">
          <svg viewBox="0 0 100 100" className="w-9 h-9" style={{ fill: C.orange }}>
            <path d="M50,15 L85,50 L50,85 L15,50 Z" />
            <path d="M50,50 L85,85 L50,90 L15,85 Z" opacity="0.6" />
          </svg>
          <span className="text-[24px] font-semibold text-gray-800" style={{ letterSpacing: "-0.5px" }}>Paper</span>
        </div>

        <h2 className="text-[20px] font-normal text-gray-700 text-center mb-8">
          {isRegister ? "Register for Paper" : "Login to Paper"}
        </h2>

        <form onSubmit={handleSubmit} className="space-y-6">
          {error && (
            <div className="bg-red-50 border border-red-200 text-red-600 px-4 py-2.5 rounded text-xs font-semibold">
              {error}
            </div>
          )}

          {/* Username Input */}
          <div>
            <input
              type="text"
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full px-4 py-3 border border-gray-200 rounded focus:border-orange-500 outline-none text-[14px] text-gray-800 placeholder-gray-400 transition-colors"
              placeholder="Phone or User ID"
            />
          </div>

          {/* Optional Email Input (Registration) */}
          {isRegister && (
            <div>
              <input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full px-4 py-3 border border-gray-200 rounded focus:border-orange-500 outline-none text-[14px] text-gray-800 placeholder-gray-400 transition-colors"
                placeholder="Email Address"
              />
            </div>
          )}

          {/* Password Input with eye toggle */}
          <div className="relative">
            <input
              type={showPassword ? "text" : "password"}
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full px-4 py-3 pr-12 border border-gray-200 rounded focus:border-orange-500 outline-none text-[14px] text-gray-800 placeholder-gray-400 transition-colors"
              placeholder="Password"
            />
            <button
              type="button"
              onClick={() => setShowPassword(!showPassword)}
              className="absolute right-3.5 top-3.5 text-gray-400 hover:text-gray-600 focus:outline-none"
            >
              {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>

          {/* Login Button */}
          <button
            type="submit"
            disabled={loading}
            className="w-full py-3 text-white rounded font-semibold text-[15px] shadow-sm flex items-center justify-center gap-2 hover:opacity-90 active:opacity-100 transition-all focus:outline-none"
            style={{ background: C.orange }}
          >
            {loading && <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />}
            {isRegister ? "Register" : "Login"}
          </button>
        </form>

        {/* Forgot Password link */}
        <div className="text-center mt-6">
          <a href="#" className="text-xs text-gray-400 hover:text-blue-500 font-medium transition-colors">
            Forgot user ID or password?
          </a>
        </div>
      </div>

      {/* External App store links */}
      <div className="flex items-center justify-center gap-6 mt-8">
        <button className="flex items-center gap-1.5 text-gray-400 hover:text-gray-600 transition-colors focus:outline-none">
          <Play size={13} className="fill-gray-400 hover:fill-gray-600" />
          <span className="text-[11px] font-semibold">Google Play</span>
        </button>
        <button className="flex items-center gap-1.5 text-gray-400 hover:text-gray-600 transition-colors focus:outline-none">
          <Apple size={14} />
          <span className="text-[11px] font-semibold">App Store</span>
        </button>
      </div>

      {/* Brand logo footer */}
      <div className="flex items-center justify-center gap-1.5 text-gray-400 text-[10px] tracking-widest font-bold mt-6 uppercase select-none opacity-80">
        <svg viewBox="0 0 100 100" className="w-4 h-4" style={{ fill: "#94a3b8" }}>
          <path d="M50,15 L85,50 L50,85 L15,50 Z" />
          <path d="M50,50 L85,85 L50,90 L15,85 Z" opacity="0.6" />
        </svg>
        <span>Paper</span>
      </div>

      {/* Toggle Register / Login */}
      <div className="text-center mt-5">
        <span className="text-xs text-gray-500 font-medium">
          {isRegister ? "Already have an account? " : "Don't have an account? "}
          <button
            onClick={() => {
              setIsRegister(!isRegister);
              setError("");
            }}
            className="text-blue-500 font-semibold hover:underline focus:outline-none"
          >
            {isRegister ? "Log in here" : "Sign up for free!"}
          </button>
        </span>
      </div>

      {/* Legal Disclaimers Footer */}
      <div className="max-w-[500px] text-[10px] text-gray-400/80 text-center leading-relaxed mt-10 space-y-1 select-none">
        <p>
          Simulated Trading Terminal: Mock execution logs representing NSE, BSE, MCX contracts. For educational and testing purposes only. No real capital risk involved.
        </p>
        <p className="text-[9px] text-gray-400/50 pt-2 font-mono">v3.0.0</p>
      </div>
    </div>
  );
}
