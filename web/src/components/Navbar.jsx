import React from 'react';
import { ShieldCheck, Key, LayoutDashboard, Terminal } from 'lucide-react';

export default function Navbar({ activeSurface, setActiveSurface, apiKey, setApiKey }) {
  return (
    <header className="bg-white border-b border-gray-200 sticky top-0 z-50">
      <div className="max-w-[1240px] mx-auto px-4 h-16 flex items-center justify-between">
        {/* Brand Logo & Surface Switcher */}
        <div className="flex items-center gap-6">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-blue-600 text-white flex items-center justify-center font-bold font-mono text-base shadow-sm">
              J
            </div>
            <div>
              <span className="font-bold text-gray-900 text-base tracking-tight">Janus</span>
              <span className="ml-1.5 px-2 py-0.5 rounded text-[10px] font-bold bg-blue-50 text-blue-700 uppercase tracking-wider">v2.0</span>
            </div>
          </div>

          {/* Surface Switcher */}
          <nav className="flex items-center gap-1 bg-gray-100 p-1 rounded-lg text-xs font-medium">
            <button
              onClick={() => setActiveSurface('customer')}
              className={`flex items-center gap-2 px-3 py-1.5 rounded-md transition-all ${
                activeSurface === 'customer'
                  ? 'bg-white text-blue-600 font-semibold shadow-sm'
                  : 'text-gray-600 hover:text-gray-900'
              }`}
            >
              <LayoutDashboard className="w-3.5 h-3.5" />
              Customer Dashboard
            </button>
            <button
              onClick={() => setActiveSurface('admin')}
              className={`flex items-center gap-2 px-3 py-1.5 rounded-md transition-all ${
                activeSurface === 'admin'
                  ? 'bg-white text-blue-600 font-semibold shadow-sm'
                  : 'text-gray-600 hover:text-gray-900'
              }`}
            >
              <Terminal className="w-3.5 h-3.5" />
              Admin Console
            </button>
          </nav>
        </div>

        {/* Right API Key Auth Input */}
        <div className="flex items-center gap-3 text-xs">
          <div className="flex items-center gap-2 bg-gray-50 border border-gray-200 rounded-lg px-3 py-1.5 focus-within:ring-2 focus-within:ring-blue-500 focus-within:border-blue-500 transition-all">
            <Key className="w-3.5 h-3.5 text-gray-400" />
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={activeSurface === 'admin' ? "Enter Admin API Key..." : "Enter Tenant API Key..."}
              className="bg-transparent text-gray-800 placeholder-gray-400 outline-none w-48 font-mono"
            />
          </div>
        </div>
      </div>
    </header>
  );
}
