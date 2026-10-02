import React, { useState, useEffect } from 'react';
import { RefreshCw, ChevronRight, Check, Clock, X, GitFork, ChevronDown } from 'lucide-react';

export default function CustomerDashboard({ apiKey, onSelectDebate }) {
  const [summary, setSummary] = useState(null);
  const [debates, setDebates] = useState([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState('');

  const fetchData = async () => {
    setLoading(true);
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
      console.error("Failed to load overview data", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, [apiKey, statusFilter]);

  // Demo fallback rows for Live Debates matching mockup
  const liveDebatesMock = [
    {
      id: "1042",
      repo_ref: "checkout-service",
      target_file: "src/validation.py",
      status: "running",
      statusLabel: "Running",
      pr_number: 87,
      duration: "2m 14s",
      currentRound: 2,
      totalRounds: 5,
      steps: [
        { label: "Review", state: "completed" },
        { label: "Patch", state: "active" },
        { label: "Gate", state: "pending" },
      ],
      progressPercent: 40,
      active: true,
    },
    {
      id: "1041",
      repo_ref: "auth-service",
      target_file: "src/jwt.py",
      status: "verifying",
      statusLabel: "Verifying",
      pr_number: 42,
      duration: "1m 32s",
      currentRound: 1,
      totalRounds: 5,
      steps: [
        { label: "Review", state: "completed" },
        { label: "Patch", state: "completed" },
        { label: "Gate", state: "clock" },
      ],
      progressPercent: 20,
      active: false,
    },
    {
      id: "1040",
      repo_ref: "billing-service",
      target_file: "src/stripe.py",
      status: "issue_found",
      statusLabel: "Issue found",
      pr_number: 104,
      duration: "3m 21s",
      currentRound: 3,
      totalRounds: 5,
      steps: [
        { label: "Review", state: "completed" },
        { label: "Patch", state: "completed" },
        { label: "Gate", state: "failed" },
      ],
      progressPercent: 60,
      active: false,
    },
    {
      id: "1039",
      repo_ref: "orders-service",
      target_file: "src/handler.py",
      status: "verified",
      statusLabel: "Verified",
      pr_number: 19,
      duration: "6m 48s",
      currentRound: 4,
      totalRounds: 5,
      steps: [
        { label: "Review", state: "completed" },
        { label: "Patch", state: "completed" },
        { label: "Gate", state: "completed" },
      ],
      progressPercent: 80,
      active: false,
    },
  ];

  // Recent outcomes mock
  const recentOutcomesMock = [
    { id: "1039", repo: "orders-service", pr: "#19", rounds: "4 / 5", verdict: "Verified", verdictType: "green", duration: "4m 12s", time: "12 min ago" },
    { id: "1038", repo: "search-service", pr: "#56", rounds: "2 / 5", verdict: "Issue found", verdictType: "red", duration: "3m 01s", time: "28 min ago" },
    { id: "1037", repo: "checkout-service", pr: "#72", rounds: "3 / 5", verdict: "Verified", verdictType: "green", duration: "5m 48s", time: "41 min ago" },
    { id: "1036", repo: "catalog-service", pr: "#11", rounds: "5 / 5", verdict: "Inconclusive", verdictType: "amber", duration: "6m 14s", time: "1 hour ago" },
    { id: "1035", repo: "billing-service", pr: "#88", rounds: "3 / 5", verdict: "Verified", verdictType: "green", duration: "4m 02s", time: "2 hours ago" },
  ];

  // System activity mock
  const systemActivityMock = [
    { role: "patcher", text: "Submitted patch for #1042", time: "2m ago", dotColor: "bg-blue-600" },
    { role: "reviewer", text: "Completed review for #1042", time: "3m ago", dotColor: "bg-gray-400" },
    { role: "gate", text: "Running tests for #1041", time: "4m ago", dotColor: "bg-gray-400" },
    { role: "system", text: "PR #1039 verified and merged", time: "8m ago", dotColor: "bg-emerald-600" },
    { role: "patcher", text: "Iteration 3 for #1040", time: "10m ago", dotColor: "bg-gray-400" },
    { role: "gate", text: "Tests failed for #1040", time: "12m ago", dotColor: "bg-red-600" },
    { role: "reviewer", text: "Started review for #1038", time: "18m ago", dotColor: "bg-gray-400" },
    { role: "system", text: "PR #1037 verified and merged", time: "24m ago", dotColor: "bg-emerald-600" },
  ];

  return (
    <div className="max-w-[1340px] mx-auto px-6 py-6 font-sans">
      {/* Page Title Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 tracking-tight">Review Operations</h1>
          <p className="text-sm text-gray-500 mt-1">Monitor live AI reviewer and patcher debates across your repositories.</p>
        </div>
        <div className="flex items-center gap-3">
          <span className="px-3 py-1 rounded-full text-xs font-semibold bg-emerald-100 text-emerald-700 flex items-center gap-1.5 border border-emerald-200">
            <span className="w-2 h-2 rounded-full bg-emerald-600"></span>
            System healthy
          </span>
          <span className="text-xs text-gray-400">Last updated 10s ago</span>
          <button
            onClick={fetchData}
            className="p-1.5 text-gray-400 hover:text-gray-700 rounded-lg hover:bg-gray-100 transition-colors"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Top 4 Metric Cards */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
        {/* Card 1: ACTIVE DEBATES */}
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm flex items-center justify-between">
          <div>
            <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">ACTIVE DEBATES</span>
            <div className="flex items-baseline gap-2 mt-2">
              <span className="text-3xl font-bold text-gray-900">{summary?.running_debates ?? 3}</span>
              <span className="text-xs text-gray-500">in progress</span>
            </div>
          </div>
          {/* Mini Bar Chart */}
          <div className="flex items-end gap-1 h-9">
            <div className="w-1.5 bg-blue-500 rounded-sm h-[40%]"></div>
            <div className="w-1.5 bg-blue-500 rounded-sm h-[70%]"></div>
            <div className="w-1.5 bg-blue-500 rounded-sm h-[90%]"></div>
            <div className="w-1.5 bg-blue-500 rounded-sm h-[100%]"></div>
            <div className="w-1.5 bg-blue-500 rounded-sm h-[60%]"></div>
          </div>
        </div>

        {/* Card 2: REVIEWS COMPLETED */}
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm flex items-center justify-between">
          <div>
            <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">REVIEWS COMPLETED</span>
            <div className="flex items-baseline gap-2 mt-2">
              <span className="text-3xl font-bold text-gray-900">{summary?.completed_debates ?? 18}</span>
              <span className="text-xs text-gray-500">in last 24h</span>
            </div>
          </div>
          {/* Mini Bar Chart */}
          <div className="flex items-end gap-1 h-9">
            <div className="w-1.5 bg-gray-300 rounded-sm h-[30%]"></div>
            <div className="w-1.5 bg-gray-300 rounded-sm h-[55%]"></div>
            <div className="w-1.5 bg-gray-300 rounded-sm h-[80%]"></div>
            <div className="w-1.5 bg-gray-300 rounded-sm h-[45%]"></div>
            <div className="w-1.5 bg-gray-300 rounded-sm h-[70%]"></div>
          </div>
        </div>

        {/* Card 3: VERIFIED PASSES */}
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm flex items-center justify-between">
          <div>
            <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">VERIFIED PASSES</span>
            <div className="flex items-baseline gap-2 mt-2">
              <span className="text-3xl font-bold text-gray-900">{summary?.pass_verdicts ?? 12}</span>
              <span className="text-xs text-gray-500">67% pass rate</span>
            </div>
          </div>
          {/* Mini Bar Chart */}
          <div className="flex items-end gap-1 h-9">
            <div className="w-1.5 bg-emerald-500 rounded-sm h-[50%]"></div>
            <div className="w-1.5 bg-emerald-500 rounded-sm h-[75%]"></div>
            <div className="w-1.5 bg-emerald-500 rounded-sm h-[95%]"></div>
            <div className="w-1.5 bg-emerald-500 rounded-sm h-[65%]"></div>
            <div className="w-1.5 bg-emerald-500 rounded-sm h-[85%]"></div>
          </div>
        </div>

        {/* Card 4: AVG. REVIEW TIME */}
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm flex items-center justify-between">
          <div>
            <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">AVG. REVIEW TIME</span>
            <div className="flex items-baseline gap-2 mt-2">
              <span className="text-3xl font-bold text-gray-900">4m</span>
              <span className="text-xs text-gray-500">+12% vs. last week</span>
            </div>
          </div>
          {/* Mini Bar Chart */}
          <div className="flex items-end gap-1 h-9">
            <div className="w-1.5 bg-gray-400 rounded-sm h-[40%]"></div>
            <div className="w-1.5 bg-gray-400 rounded-sm h-[60%]"></div>
            <div className="w-1.5 bg-gray-400 rounded-sm h-[80%]"></div>
            <div className="w-1.5 bg-gray-400 rounded-sm h-[50%]"></div>
            <div className="w-1.5 bg-gray-400 rounded-sm h-[70%]"></div>
          </div>
        </div>
      </div>

      {/* Main Content Layout Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
        {/* Left Column (8 cols) */}
        <div className="lg:col-span-8 space-y-6">
          {/* Card A: Live debates */}
          <div className="bg-white border border-gray-200 rounded-xl p-6 shadow-sm">
            <div className="flex items-center justify-between mb-5">
              <div>
                <h2 className="text-lg font-bold text-gray-900">Live debates</h2>
                <p className="text-xs text-gray-500 mt-0.5">Real-time view of ongoing AI reviewer and patcher debates.</p>
              </div>
              <div className="flex items-center gap-2 border border-gray-200 rounded-lg px-3 py-1.5 text-xs text-gray-700 bg-white shadow-sm cursor-pointer">
                <span>All statuses</span>
                <ChevronDown className="w-3.5 h-3.5 text-gray-400" />
              </div>
            </div>

            {/* List of Live Debate Cards */}
            <div className="space-y-3">
              {liveDebatesMock.map((item) => (
                <div
                  key={item.id}
                  onClick={() => onSelectDebate(item.id)}
                  className={`p-4 rounded-xl border transition-all cursor-pointer flex items-center justify-between gap-3 ${
                    item.active
                      ? 'border-2 border-blue-500 bg-blue-50/10 shadow-sm'
                      : 'border-gray-200 hover:border-gray-300 bg-white'
                  }`}
                >
                  {/* Left Info Column */}
                  <div className="flex items-center gap-3 min-w-[210px]">
                    <div className={`w-2.5 h-2.5 rounded-full shrink-0 ${item.active ? 'bg-blue-600' : 'bg-gray-300'}`}></div>
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="font-bold text-sm text-gray-900">#{item.id}</span>
                        <span className="font-bold text-sm text-gray-900">{item.repo_ref}</span>
                      </div>
                      <div className="flex items-center gap-2 mt-0.5">
                        <span className="text-xs text-gray-400 font-mono">{item.duration}</span>
                        <span className="text-xs text-gray-400 font-mono">{item.target_file}</span>
                      </div>
                    </div>
                  </div>

                  {/* PR Badge */}
                  <div className="flex items-center gap-1 bg-gray-50 border border-gray-200 px-2.5 py-1 rounded text-xs font-mono text-gray-700 shrink-0">
                    <span>PR #{item.pr_number}</span>
                    <GitFork className="w-3 h-3 text-gray-400" />
                  </div>

                  {/* Inline Stepper Component */}
                  <div className="flex items-center gap-2 shrink-0">
                    {item.steps.map((st, idx) => (
                      <React.Fragment key={idx}>
                        <div className="flex items-center gap-1">
                          {st.state === 'completed' && (
                            <div className="w-5 h-5 rounded-full bg-emerald-600 text-white flex items-center justify-center text-[10px]">
                              <Check className="w-3.5 h-3.5 stroke-[3]" />
                            </div>
                          )}
                          {st.state === 'active' && (
                            <div className="w-5 h-5 rounded-full bg-blue-600 text-white flex items-center justify-center text-[10px] animate-pulse-ring ring-2 ring-blue-100">
                              <div className="w-2 h-2 rounded-full bg-white"></div>
                            </div>
                          )}
                          {st.state === 'clock' && (
                            <div className="w-5 h-5 rounded-full border border-gray-300 text-gray-500 flex items-center justify-center text-[10px]">
                              <Clock className="w-3 h-3" />
                            </div>
                          )}
                          {st.state === 'failed' && (
                            <div className="w-5 h-5 rounded-full bg-red-600 text-white flex items-center justify-center text-[10px]">
                              <X className="w-3.5 h-3.5 stroke-[3]" />
                            </div>
                          )}
                          {st.state === 'pending' && (
                            <div className="w-5 h-5 rounded-full border border-gray-300 text-gray-400 flex items-center justify-center text-[10px]">
                              ◯
                            </div>
                          )}
                          <span className="text-xs font-semibold text-gray-600">{st.label}</span>
                        </div>
                        {idx < item.steps.length - 1 && (
                          <div className={`w-8 h-[2px] ${st.state === 'completed' ? 'bg-emerald-600' : 'bg-gray-200 border-t border-dashed'}`}></div>
                        )}
                      </React.Fragment>
                    ))}
                  </div>

                  {/* Round Progress & Bar */}
                  <div className="w-24 shrink-0">
                    <div className="text-[11px] font-semibold text-gray-500 mb-1">
                      Round {item.currentRound} / {item.totalRounds}
                    </div>
                    <div className="w-full bg-gray-100 h-1.5 rounded-full overflow-hidden">
                      <div
                        className={`h-full ${
                          item.status === 'issue_found' ? 'bg-red-500' :
                          item.status === 'verified' ? 'bg-emerald-500' : 'bg-blue-600'
                        }`}
                        style={{ width: `${item.progressPercent}%` }}
                      ></div>
                    </div>
                  </div>

                  {/* Status Badge */}
                  <div className="shrink-0">
                    <span className={`px-2.5 py-1 rounded-full text-xs font-semibold flex items-center gap-1.5 ${
                      item.status === 'running' ? 'bg-blue-100 text-blue-700' :
                      item.status === 'verifying' ? 'bg-gray-100 text-gray-700' :
                      item.status === 'issue_found' ? 'bg-red-100 text-red-700' : 'bg-emerald-100 text-emerald-700'
                    }`}>
                      <span className={`w-1.5 h-1.5 rounded-full ${
                        item.status === 'running' ? 'bg-blue-600' :
                        item.status === 'verifying' ? 'bg-gray-500' :
                        item.status === 'issue_found' ? 'bg-red-600' : 'bg-emerald-600'
                      }`}></span>
                      {item.statusLabel}
                    </span>
                  </div>

                  <ChevronRight className="w-4 h-4 text-gray-400 group-hover:text-gray-700 shrink-0" />
                </div>
              ))}
            </div>
          </div>

          {/* Card B: Recent outcomes Table */}
          <div className="bg-white border border-gray-200 rounded-xl p-6 shadow-sm">
            <div className="mb-5">
              <h2 className="text-lg font-bold text-gray-900">Recent outcomes</h2>
              <p className="text-xs text-gray-500 mt-0.5">Latest completed debates and their results.</p>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs font-sans">
                <thead className="bg-gray-50/80 text-gray-400 font-semibold uppercase text-[10px] tracking-wider border-b border-gray-100">
                  <tr>
                    <th className="py-3 px-3">DEBATE ID</th>
                    <th className="py-3 px-3">REPOSITORY</th>
                    <th className="py-3 px-3">PR</th>
                    <th className="py-3 px-3">ROUNDS</th>
                    <th className="py-3 px-3">VERDICT</th>
                    <th className="py-3 px-3">DURATION</th>
                    <th className="py-3 px-3">COMPLETED</th>
                    <th className="py-3 px-3"></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {recentOutcomesMock.map((row) => (
                    <tr
                      key={row.id}
                      onClick={() => onSelectDebate(row.id)}
                      className="hover:bg-gray-50/80 cursor-pointer transition-colors"
                    >
                      <td className="py-3.5 px-3 font-mono font-bold text-gray-900">#{row.id}</td>
                      <td className="py-3.5 px-3 font-semibold text-gray-900">{row.repo}</td>
                      <td className="py-3.5 px-3 font-mono text-gray-500">{row.pr}</td>
                      <td className="py-3.5 px-3 font-mono text-gray-600">{row.rounds}</td>
                      <td className="py-3.5 px-3">
                        <span className={`px-2.5 py-0.5 rounded-md text-xs font-semibold ${
                          row.verdictType === 'green' ? 'bg-emerald-100 text-emerald-700' :
                          row.verdictType === 'red' ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'
                        }`}>
                          {row.verdict}
                        </span>
                      </td>
                      <td className="py-3.5 px-3 font-mono text-gray-500">{row.duration}</td>
                      <td className="py-3.5 px-3 font-mono text-gray-400">{row.time}</td>
                      <td className="py-3.5 px-3 text-right">
                        <ChevronRight className="w-4 h-4 text-gray-400 ml-auto" />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        {/* Right Sidebar Column (4 cols) */}
        <div className="lg:col-span-4 space-y-6">
          {/* Card 1: System activity */}
          <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
            <div className="flex items-center justify-between mb-4">
              <h3 className="font-bold text-gray-900 text-sm">System activity</h3>
              <div className="flex items-center gap-1 border border-gray-200 rounded-lg px-2.5 py-1 text-xs text-gray-600 bg-white cursor-pointer">
                <span>All events</span>
                <ChevronDown className="w-3.5 h-3.5 text-gray-400" />
              </div>
            </div>

            <div className="space-y-3.5 text-xs">
              {systemActivityMock.map((act, idx) => (
                <div key={idx} className="flex items-center justify-between">
                  <div className="flex items-center gap-2.5">
                    <span className={`w-2 h-2 rounded-full ${act.dotColor}`}></span>
                    <span className="font-semibold text-gray-900 w-16">{act.role}</span>
                    <span className="text-gray-600 truncate max-w-[170px]">{act.text}</span>
                  </div>
                  <span className="text-gray-400 font-mono text-[11px] shrink-0">{act.time}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Card 2: Verification (last 24h) */}
          <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
            <h3 className="font-bold text-gray-900 text-sm mb-4">
              Verification <span className="text-xs font-normal text-gray-400">(last 24h)</span>
            </h3>

            {/* Segmented Progress Bar */}
            <div className="w-full bg-gray-100 h-3 rounded-full flex overflow-hidden mb-5">
              <div className="bg-emerald-500 h-full" style={{ width: '67%' }}></div>
              <div className="bg-red-500 h-full" style={{ width: '22%' }}></div>
              <div className="bg-amber-400 h-full" style={{ width: '11%' }}></div>
            </div>

            {/* Stats Breakdown Grid */}
            <div className="grid grid-cols-4 gap-2 text-xs">
              <div>
                <div className="text-base font-bold text-gray-900">12</div>
                <div className="text-[11px] text-gray-500">Verified</div>
                <div className="text-[11px] text-gray-400">67%</div>
              </div>
              <div>
                <div className="text-base font-bold text-gray-900">4</div>
                <div className="text-[11px] text-gray-500">Issue found</div>
                <div className="text-[11px] text-gray-400">22%</div>
              </div>
              <div>
                <div className="text-base font-bold text-gray-900">2</div>
                <div className="text-[11px] text-gray-500">Inconclusive</div>
                <div className="text-[11px] text-gray-400">11%</div>
              </div>
              <div>
                <div className="text-base font-bold text-gray-900">18</div>
                <div className="text-[11px] text-gray-500">Total</div>
                <div className="text-[11px] text-gray-400">-</div>
              </div>
            </div>
          </div>

          {/* Card 3: Average time by stage (last 24h) */}
          <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
            <h3 className="font-bold text-gray-900 text-sm mb-4">
              Average time by stage <span className="text-xs font-normal text-gray-400">(last 24h)</span>
            </h3>

            <div className="space-y-4 text-xs">
              {/* Review */}
              <div>
                <div className="flex justify-between font-semibold text-gray-800 mb-1.5">
                  <span>Review</span>
                  <span className="font-mono text-gray-500">1m 12s</span>
                </div>
                <div className="w-full bg-gray-100 h-2.5 rounded-full overflow-hidden">
                  <div className="bg-blue-500 h-full rounded-full" style={{ width: '40%' }}></div>
                </div>
              </div>

              {/* Patch */}
              <div>
                <div className="flex justify-between font-semibold text-gray-800 mb-1.5">
                  <span>Patch</span>
                  <span className="font-mono text-gray-500">2m 08s</span>
                </div>
                <div className="w-full bg-gray-100 h-2.5 rounded-full overflow-hidden">
                  <div className="bg-blue-500 h-full rounded-full" style={{ width: '75%' }}></div>
                </div>
              </div>

              {/* Gate */}
              <div>
                <div className="flex justify-between font-semibold text-gray-800 mb-1.5">
                  <span>Gate</span>
                  <span className="font-mono text-gray-500">48s</span>
                </div>
                <div className="w-full bg-gray-100 h-2.5 rounded-full overflow-hidden">
                  <div className="bg-blue-500 h-full rounded-full" style={{ width: '25%' }}></div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
