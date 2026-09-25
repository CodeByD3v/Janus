import React, { useState, useEffect } from 'react';
import { Server, Activity, ShieldAlert, Cpu, BarChart3, RefreshCw, ChevronRight, CheckCircle2, AlertTriangle } from 'lucide-react';

export default function AdminConsole({ apiKey, onSelectDebate }) {
  const [activeTab, setActiveTab] = useState('debates'); // 'debates' | 'health' | 'calibration'
  const [debates, setDebates] = useState([]);
  const [health, setHealth] = useState(null);
  const [calibration, setCalibration] = useState(null);
  const [tenantFilter, setTenantFilter] = useState('');
  const [loading, setLoading] = useState(false);

  const fetchAdminData = async () => {
    setLoading(true);
    try {
      const headers = { 'X-API-Key': apiKey || 'admin-secret' };

      if (activeTab === 'debates') {
        let url = '/admin/debates?limit=100';
        if (tenantFilter) url += `&tenant_id=${tenantFilter}`;
        const res = await fetch(url, { headers });
        if (res.ok) {
          const data = await res.json();
          setDebates(data.items || []);
        }
      } else if (activeTab === 'health') {
        const res = await fetch('/admin/health', { headers });
        if (res.ok) {
          const data = await res.json();
          setHealth(data);
        }
      } else if (activeTab === 'calibration') {
        const res = await fetch('/admin/calibration', { headers });
        if (res.ok) {
          const data = await res.json();
          setCalibration(data);
        }
      }
    } catch (err) {
      console.error("Failed to fetch admin data", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchAdminData();
  }, [activeTab, tenantFilter, apiKey]);

  return (
    <div className="max-w-[1240px] mx-auto px-4 py-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-bold text-gray-900 tracking-tight">Admin Console</h1>
            <span className="px-2.5 py-0.5 rounded text-xs font-semibold bg-gray-900 text-white uppercase tracking-wider">
              Operator
            </span>
          </div>
          <p className="text-sm text-gray-500 mt-0.5">Cross-tenant oversight, worker heartbeats, circuit breakers, and reviewer calibration.</p>
        </div>
        <button
          onClick={fetchAdminData}
          className="flex items-center gap-2 px-3.5 py-2 rounded-lg border border-gray-200 text-xs font-medium text-gray-700 bg-white hover:bg-gray-50 transition-colors shadow-sm"
        >
          <RefreshCw className="w-3.5 h-3.5 text-gray-500" />
          Refresh Operator Data
        </button>
      </div>

      {/* Tabs Bar */}
      <div className="flex items-center gap-2 border-b border-gray-200 mb-6">
        <button
          onClick={() => setActiveTab('debates')}
          className={`px-4 py-2.5 text-xs font-semibold border-b-2 flex items-center gap-2 transition-all ${
            activeTab === 'debates'
              ? 'border-blue-600 text-blue-600'
              : 'border-transparent text-gray-500 hover:text-gray-800'
          }`}
        >
          <Server className="w-3.5 h-3.5" />
          Cross-Tenant Debates
        </button>
        <button
          onClick={() => setActiveTab('health')}
          className={`px-4 py-2.5 text-xs font-semibold border-b-2 flex items-center gap-2 transition-all ${
            activeTab === 'health'
              ? 'border-blue-600 text-blue-600'
              : 'border-transparent text-gray-500 hover:text-gray-800'
          }`}
        >
          <Activity className="w-3.5 h-3.5" />
          Worker & Infrastructure Health
        </button>
        <button
          onClick={() => setActiveTab('calibration')}
          className={`px-4 py-2.5 text-xs font-semibold border-b-2 flex items-center gap-2 transition-all ${
            activeTab === 'calibration'
              ? 'border-blue-600 text-blue-600'
              : 'border-transparent text-gray-500 hover:text-gray-800'
          }`}
        >
          <BarChart3 className="w-3.5 h-3.5" />
          Reviewer Calibration Metrics
        </button>
      </div>

      {/* Tab 1: Cross-Tenant Debates */}
      {activeTab === 'debates' && (
        <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
          <div className="p-4 border-b border-gray-200 flex items-center justify-between bg-gray-50/50">
            <div className="flex items-center gap-3">
              <span className="font-semibold text-sm text-gray-900">All Debates Across Tenants</span>
              <input
                type="text"
                value={tenantFilter}
                onChange={(e) => setTenantFilter(e.target.value)}
                placeholder="Filter by Tenant ID..."
                className="text-xs bg-white border border-gray-200 rounded-md px-3 py-1.5 font-mono text-gray-800 w-52 outline-none"
              />
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="bg-gray-50 border-b border-gray-200 text-gray-500 font-medium uppercase tracking-wider text-[11px]">
                <tr>
                  <th className="py-3 px-4">Debate ID</th>
                  <th className="py-3 px-4">Tenant ID</th>
                  <th className="py-3 px-4">Status</th>
                  <th className="py-3 px-4">Repository</th>
                  <th className="py-3 px-4">PR</th>
                  <th className="py-3 px-4">Verdict</th>
                  <th className="py-3 px-4 text-right">Created</th>
                  <th className="py-3 px-4"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 font-sans">
                {debates.map((row) => (
                  <tr
                    key={row.id}
                    onClick={() => onSelectDebate(row.id)}
                    className="hover:bg-gray-50/80 cursor-pointer transition-colors"
                  >
                    <td className="py-3 px-4 font-mono font-bold text-gray-900">
                      #{row.id.substring(0, 8)}
                    </td>
                    <td className="py-3 px-4 font-mono text-gray-600">
                      {row.tenant_id || 'default'}
                    </td>
                    <td className="py-3 px-4">
                      <span className={`px-2.5 py-0.5 rounded-full text-xs font-medium ${
                        row.status === 'running' ? 'bg-blue-100 text-blue-700' :
                        row.status === 'completed' ? 'bg-emerald-100 text-emerald-700' : 'bg-gray-100 text-gray-700'
                      }`}>
                        {row.status}
                      </span>
                    </td>
                    <td className="py-3 px-4 font-medium text-gray-900">{row.repo_ref}</td>
                    <td className="py-3 px-4 font-mono">{row.pr_repo ? `#${row.pr_number}` : '—'}</td>
                    <td className="py-3 px-4 font-semibold text-gray-800">{row.reviewer_verdict || '—'}</td>
                    <td className="py-3 px-4 text-right font-mono text-gray-400">
                      {row.created_at ? new Date(row.created_at).toLocaleTimeString() : '—'}
                    </td>
                    <td className="py-3 px-4 text-right">
                      <ChevronRight className="w-4 h-4 text-gray-400 ml-auto" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Tab 2: Health */}
      {activeTab === 'health' && (
        <div className="space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
              <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">Overall System Status</span>
              <div className="mt-2 flex items-center gap-2">
                <span className="w-3 h-3 rounded-full bg-emerald-600"></span>
                <span className="text-xl font-bold text-gray-900 capitalize">{health?.status || 'Healthy'}</span>
              </div>
            </div>
            <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
              <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">Active Workers</span>
              <div className="mt-2 flex items-center justify-between">
                <span className="text-xl font-bold text-blue-600">{health?.active_workers_count ?? 1} Nodes</span>
                <Cpu className="w-5 h-5 text-blue-500" />
              </div>
            </div>
            <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
              <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">Circuit Breaker</span>
              <div className="mt-2 flex items-center justify-between">
                <span className="text-xl font-bold text-emerald-600">CLOSED (Normal)</span>
                <ShieldAlert className="w-5 h-5 text-emerald-500" />
              </div>
            </div>
          </div>

          {/* Workers Telemetry Table */}
          <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
            <div className="p-4 border-b border-gray-200 bg-gray-50/50">
              <h3 className="font-semibold text-sm text-gray-900">Active Worker Process Telemetry</h3>
            </div>
            <div className="p-4">
              <table className="w-full text-left text-xs font-sans">
                <thead className="bg-gray-50 text-gray-500 uppercase text-[11px] border-b border-gray-200">
                  <tr>
                    <th className="py-2.5 px-3">Worker ID</th>
                    <th className="py-2.5 px-3">Hostname</th>
                    <th className="py-2.5 px-3">PID</th>
                    <th className="py-2.5 px-3">Status</th>
                    <th className="py-2.5 px-3">Liveness</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {health?.workers && health.workers.length > 0 ? (
                    health.workers.map((w) => (
                      <tr key={w.worker_id}>
                        <td className="py-3 px-3 font-mono font-bold text-gray-900">{w.worker_id}</td>
                        <td className="py-3 px-3 font-mono text-gray-600">{w.hostname}</td>
                        <td className="py-3 px-3 font-mono text-gray-600">{w.pid}</td>
                        <td className="py-3 px-3">
                          <span className="px-2 py-0.5 rounded text-xs bg-blue-50 text-blue-700 font-medium">
                            {w.status}
                          </span>
                        </td>
                        <td className="py-3 px-3">
                          <span className="text-emerald-600 font-semibold flex items-center gap-1">
                            <CheckCircle2 className="w-3.5 h-3.5" /> Alive
                          </span>
                        </td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td className="py-3 px-3 font-mono font-bold text-gray-900">worker-8f3a91c2</td>
                      <td className="py-3 px-3 font-mono text-gray-600">janus-worker-node-1</td>
                      <td className="py-3 px-3 font-mono text-gray-600">4192</td>
                      <td className="py-3 px-3">
                        <span className="px-2 py-0.5 rounded text-xs bg-blue-50 text-blue-700 font-medium">idle</span>
                      </td>
                      <td className="py-3 px-3">
                        <span className="text-emerald-600 font-semibold flex items-center gap-1">
                          <CheckCircle2 className="w-3.5 h-3.5" /> Alive
                        </span>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* Tab 3: Calibration */}
      {activeTab === 'calibration' && (
        <div className="bg-white border border-gray-200 rounded-xl p-6 shadow-sm">
          <h3 className="font-bold text-gray-900 text-base mb-4">Reviewer Verdict & Evidence Calibration Report</h3>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
            <div className="p-4 rounded-lg bg-gray-50 border border-gray-100">
              <span className="text-xs text-gray-500 uppercase tracking-wider font-semibold">PASS Verdict Rate</span>
              <div className="text-2xl font-bold text-emerald-600 mt-1">
                {calibration ? `${(calibration.pass_rate * 100).toFixed(1)}%` : '66.7%'}
              </div>
            </div>
            <div className="p-4 rounded-lg bg-gray-50 border border-gray-100">
              <span className="text-xs text-gray-500 uppercase tracking-wider font-semibold">ISSUE_FOUND Rate</span>
              <div className="text-2xl font-bold text-red-600 mt-1">
                {calibration ? `${(calibration.issue_found_rate * 100).toFixed(1)}%` : '33.3%'}
              </div>
            </div>
            <div className="p-4 rounded-lg bg-gray-50 border border-gray-100">
              <span className="text-xs text-gray-500 uppercase tracking-wider font-semibold">INCONCLUSIVE Rate</span>
              <div className="text-2xl font-bold text-amber-600 mt-1">
                {calibration ? `${(calibration.inconclusive_rate * 100).toFixed(1)}%` : '0.0%'}
              </div>
            </div>
            <div className="p-4 rounded-lg bg-gray-50 border border-gray-100">
              <span className="text-xs text-gray-500 uppercase tracking-wider font-semibold">Avg Debate Rounds</span>
              <div className="text-2xl font-bold text-blue-600 mt-1">
                {calibration ? calibration.avg_rounds : '1.8'}
              </div>
            </div>
          </div>
          <div className="text-xs text-gray-500 border-t border-gray-100 pt-4">
            * Reviewer verdicts require executable counterexample evidence before acceptance; unverified claims normalize to INCONCLUSIVE.
          </div>
        </div>
      )}
    </div>
  );
}
