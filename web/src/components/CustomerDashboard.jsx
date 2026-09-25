import React, { useState, useEffect } from 'react';
import { Play, CheckCircle2, AlertCircle, GitPullRequest, Search, RefreshCw, ChevronRight } from 'lucide-react';

export default function CustomerDashboard({ apiKey, onSelectDebate }) {
  const [summary, setSummary] = useState(null);
  const [debates, setDebates] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [statusFilter, setStatusFilter] = useState('');

  const fetchData = async () => {
    setLoading(true);
    setError('');
    try {
      const headers = { 'X-API-Key': apiKey || 'test-key' };
      
      const summaryRes = await fetch('/debates/summary', { headers });
      if (summaryRes.ok) {
        const sData = await summaryRes.json();
        setSummary(sData);
      }

      let debatesUrl = '/debates?limit=50';
      if (statusFilter) debatesUrl += `&status=${statusFilter}`;
      const debatesRes = await fetch(debatesUrl, { headers });
      if (debatesRes.ok) {
        const dData = await debatesRes.json();
        setDebates(dData.items || []);
      }
    } catch (err) {
      setError(err.message || 'Failed to load debates');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, [apiKey, statusFilter]);

  // Fallback demo rows if initial db is empty
  const displayRows = debates.length > 0 ? debates : [
    {
      id: "1024",
      repo_ref: "acme-inc/upload-service",
      target_file: "src/upload.py",
      status: "running",
      reviewer_verdict: "ISSUE_FOUND",
      pr_repo: "acme-inc/upload-service",
      pr_number: 87,
      created_at: "2026-09-23T14:32:00Z"
    },
    {
      id: "1023",
      repo_ref: "acme-inc/auth-api",
      target_file: "auth/jwt.py",
      status: "completed",
      reviewer_verdict: "PASS",
      merged: true,
      pr_repo: "acme-inc/auth-api",
      pr_number: 42,
      created_at: "2026-09-23T12:15:00Z"
    },
    {
      id: "1022",
      repo_ref: "acme-inc/billing",
      target_file: "stripe/webhook.py",
      status: "completed",
      reviewer_verdict: "INCONCLUSIVE",
      merged: false,
      pr_repo: "acme-inc/billing",
      pr_number: 104,
      created_at: "2026-09-22T18:40:00Z"
    }
  ];

  return (
    <div className="max-w-[1240px] mx-auto px-4 py-6">
      {/* Top Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 tracking-tight">Debates Overview</h1>
          <p className="text-sm text-gray-500">Monitor live AI reviewer & patcher debates across your repository PRs.</p>
        </div>
        <button
          onClick={fetchData}
          className="flex items-center gap-2 px-3.5 py-2 rounded-lg border border-gray-200 text-xs font-medium text-gray-700 bg-white hover:bg-gray-50 transition-colors shadow-sm"
        >
          <RefreshCw className="w-3.5 h-3.5 text-gray-500" />
          Refresh
        </button>
      </div>

      {/* Summary Number Cards */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">Total Debates</span>
          <div className="mt-2 flex items-baseline justify-between">
            <span className="text-2xl font-bold text-gray-900">{summary?.total_debates ?? displayRows.length}</span>
            <span className="text-xs text-gray-400">All time</span>
          </div>
        </div>

        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">Active Live Debates</span>
          <div className="mt-2 flex items-baseline justify-between">
            <span className="text-2xl font-bold text-blue-600 flex items-center gap-2">
              <span className="w-2.5 h-2.5 rounded-full bg-blue-600 animate-pulse"></span>
              {summary?.running_debates ?? 1}
            </span>
            <span className="text-xs text-blue-600 font-medium">In progress</span>
          </div>
        </div>

        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">Pass Rate</span>
          <div className="mt-2 flex items-baseline justify-between">
            <span className="text-2xl font-bold text-emerald-600">
              {summary ? `${Math.round((summary.pass_verdicts / (summary.total_debates || 1)) * 100)}%` : '67%'}
            </span>
            <span className="text-xs text-emerald-600 font-medium">Verified PASS</span>
          </div>
        </div>

        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">Merged PRs</span>
          <div className="mt-2 flex items-baseline justify-between">
            <span className="text-2xl font-bold text-purple-600">
              {summary?.merged_count ?? 1}
            </span>
            <span className="text-xs text-purple-600 font-medium">Auto-merged</span>
          </div>
        </div>
      </div>

      {/* Debates Table Card */}
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        {/* Table Filters Header */}
        <div className="p-4 border-b border-gray-200 flex items-center justify-between bg-gray-50/50">
          <div className="flex items-center gap-3">
            <span className="font-semibold text-sm text-gray-900">Debates</span>
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="text-xs bg-white border border-gray-200 rounded-md px-2.5 py-1.5 font-medium text-gray-700 outline-none"
            >
              <option value="">All Statuses</option>
              <option value="running">Running (Live)</option>
              <option value="completed">Completed</option>
              <option value="queued">Queued</option>
              <option value="error">Error</option>
            </select>
          </div>
        </div>

        {/* Table View */}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="bg-gray-50 border-b border-gray-200 text-gray-500 font-medium uppercase tracking-wider text-[11px]">
              <tr>
                <th className="py-3 px-4">Debate ID</th>
                <th className="py-3 px-4">Status</th>
                <th className="py-3 px-4">Repository & Target</th>
                <th className="py-3 px-4">Pull Request</th>
                <th className="py-3 px-4">Verdict</th>
                <th className="py-3 px-4 text-right">Created</th>
                <th className="py-3 px-4"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 font-sans">
              {displayRows.map((row) => {
                const isRunning = row.status === 'running';
                return (
                  <tr
                    key={row.id}
                    onClick={() => onSelectDebate(row.id)}
                    className="hover:bg-gray-50/80 cursor-pointer transition-colors group"
                  >
                    <td className="py-3.5 px-4 font-mono font-bold text-gray-900">
                      #{row.id.substring(0, 8)}
                    </td>
                    <td className="py-3.5 px-4">
                      {isRunning ? (
                        <span className="px-2.5 py-1 rounded-full text-xs font-semibold bg-blue-100 text-blue-700 flex items-center gap-1.5 w-fit">
                          <span className="w-2 h-2 rounded-full bg-blue-600 animate-pulse"></span>
                          Running (Live)
                        </span>
                      ) : row.status === 'completed' ? (
                        <span className="px-2.5 py-1 rounded-full text-xs font-medium bg-emerald-100 text-emerald-700 w-fit">
                          Completed
                        </span>
                      ) : (
                        <span className="px-2.5 py-1 rounded-full text-xs font-medium bg-gray-100 text-gray-700 w-fit">
                          {row.status}
                        </span>
                      )}
                    </td>
                    <td className="py-3.5 px-4">
                      <div className="font-semibold text-gray-900">{row.repo_ref}</div>
                      <div className="text-[11px] text-gray-400 font-mono mt-0.5">{row.target_file}</div>
                    </td>
                    <td className="py-3.5 px-4 font-mono">
                      {row.pr_repo ? `${row.pr_repo} #${row.pr_number}` : '—'}
                    </td>
                    <td className="py-3.5 px-4 font-semibold">
                      {row.reviewer_verdict === 'PASS' && <span className="text-emerald-600">PASS</span>}
                      {row.reviewer_verdict === 'ISSUE_FOUND' && <span className="text-red-600">ISSUE_FOUND</span>}
                      {row.reviewer_verdict === 'INCONCLUSIVE' && <span className="text-amber-600">INCONCLUSIVE</span>}
                      {!row.reviewer_verdict && <span className="text-gray-400">—</span>}
                    </td>
                    <td className="py-3.5 px-4 text-right text-gray-500 font-mono">
                      {row.created_at ? new Date(row.created_at).toLocaleTimeString() : 'Just now'}
                    </td>
                    <td className="py-3.5 px-4 text-right">
                      <ChevronRight className="w-4 h-4 text-gray-400 group-hover:text-blue-600 transition-colors ml-auto" />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
