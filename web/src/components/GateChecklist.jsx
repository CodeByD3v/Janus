import React from 'react';
import { CheckCircle2, XCircle, ChevronRight, ShieldCheck } from 'lucide-react';

export default function GateChecklist({ roundNum = 1, gateResult, onViewLogs }) {
  const defaultChecks = [
    { check: 'Linter', passed: true, duration: '12s' },
    { check: 'Type Check', passed: true, duration: '18s' },
    { check: 'Tests', passed: false, duration: '34s' },
    { check: 'Security Scan', passed: true, duration: '9s' },
  ];

  const checks = gateResult && gateResult.checks ? gateResult.checks : defaultChecks;
  const overallPassed = gateResult ? gateResult.passed : false;

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 mb-5 shadow-sm">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <h3 className="font-semibold text-gray-900 text-sm">Gate Results</h3>
          <span className="text-xs text-gray-400">Round {roundNum}</span>
        </div>
        <span className={`px-2.5 py-0.5 rounded-full text-xs font-semibold flex items-center gap-1.5 ${
          overallPassed ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'
        }`}>
          <span className={`w-1.5 h-1.5 rounded-full ${overallPassed ? 'bg-green-600' : 'bg-red-600'}`}></span>
          {overallPassed ? 'Passed' : 'Failed'}
        </span>
      </div>

      {/* Checklist items */}
      <div className="space-y-3 mb-4">
        {checks.map((item, idx) => (
          <div key={idx} className="flex items-center justify-between py-1 border-b border-gray-50 last:border-0 text-xs">
            <div className="flex items-center gap-2.5">
              {item.passed ? (
                <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" />
              ) : (
                <XCircle className="w-4 h-4 text-red-600 shrink-0" />
              )}
              <span className="font-medium text-gray-800">{item.check}</span>
            </div>
            <div className="flex items-center gap-3">
              <span className={`font-semibold ${item.passed ? 'text-emerald-600' : 'text-red-600'}`}>
                {item.passed ? 'Passed' : 'Failed'}
              </span>
              <span className="text-gray-400 font-mono text-[11px]">
                {item.duration || '10s'}
              </span>
            </div>
          </div>
        ))}
      </div>

      {/* View logs action button */}
      <button
        onClick={onViewLogs}
        className="w-full flex items-center justify-between px-3.5 py-2 rounded-lg border border-gray-200 text-xs text-gray-700 hover:bg-gray-50 font-medium transition-colors"
      >
        <span>View logs</span>
        <ChevronRight className="w-4 h-4 text-gray-400" />
      </button>
    </div>
  );
}
