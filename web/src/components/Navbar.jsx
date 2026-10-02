import React, { useState } from 'react';
import { Key, LayoutDashboard, Terminal, ChevronDown, Box } from 'lucide-react';

export default function Navbar({ activeSurface, setActiveSurface, apiKey, setApiKey }) {
  const [showKeyModal, setShowKeyModal] = useState(false);

  return (
    <header className="bg-white border-b border-gray-200 sticky top-0 z-50">
      <div className="max-w-[1340px] mx-auto px-6 h-16 flex items-center justify-between">
        {/* Brand Logo */}
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-gray-900 text-white flex items-center justify-center shadow-sm">
            <Box className="w-5 h-5 stroke-[2]" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <span className="font-bold text-gray-900 text-lg tracking-tight">Janus</span>
            </div>
            <span className="text-[11px] text-gray-500 font-normal">Adversarial AI for Safer Code</span>
          </div>
        </div>

        {/* Center / Right Nav Elements */}
        <div className="flex items-center gap-5">
          {/* Surface Switcher Pills */}
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
              Overview
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

          {/* Key Auth Toggle */}
          <div className="relative">
            <button
              onClick={() => setShowKeyModal(!showKeyModal)}
              className="p-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 text-gray-600 transition-colors flex items-center gap-1 text-xs"
              title="API Key Settings"
            >
              <Key className="w-4 h-4 text-gray-500" />
            </button>

            {showKeyModal && (
              <div className="absolute right-0 mt-2 w-64 bg-white border border-gray-200 rounded-xl shadow-lg p-3 z-50">
                <label className="block text-[11px] font-semibold text-gray-700 mb-1">X-API-Key Header</label>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="Enter API Key..."
                  className="w-full bg-gray-50 border border-gray-200 rounded-lg px-2.5 py-1.5 text-xs text-gray-900 outline-none font-mono"
                />
              </div>
            )}
          </div>

          {/* User / Admin Menu */}
          <div className="flex items-center gap-2 text-xs font-medium text-gray-700 pl-2 border-l border-gray-200">
            <span>Admin</span>
            <div className="w-7 h-7 rounded-full bg-gray-100 text-gray-900 font-bold flex items-center justify-center text-xs border border-gray-200">
              J
            </div>
            <ChevronDown className="w-3.5 h-3.5 text-gray-400" />
          </div>
        </div>
      </div>
    </header>
  );
}
