import React, { useState, useEffect } from 'react';
import { ArrowLeft, ChevronDown, ChevronUp, ExternalLink, ShieldAlert, Check, Clock } from 'lucide-react';
import PipelineStepper from './PipelineStepper';
import DiffViewer from './DiffViewer';
import GateChecklist from './GateChecklist';
import ActivityTimeline from './ActivityTimeline';

export default function LiveDebateView({ debateId = "1024", apiKey = "", onBack }) {
  const [debate, setDebate] = useState(null);
  const [loading, setLoading] = useState(true);
  const [expandedRounds, setExpandedRounds] = useState({});

  useEffect(() => {
    // Fetch initial debate details
    const fetchDebate = async () => {
      try {
        const res = await fetch(`/debates/${debateId}`, {
          headers: { 'X-API-Key': apiKey }
        });
        if (res.ok) {
          const data = await res.json();
          setDebate(data);
        }
      } catch (err) {
        console.error("Failed to fetch debate", err);
      } finally {
        setLoading(false);
      }
    };

    fetchDebate();

    // Setup SSE live stream listener
    const eventSource = new EventSource(`/debates/${debateId}/stream`);
    
    eventSource.addEventListener('round', (e) => {
      const roundData = JSON.parse(e.data);
      setDebate((prev) => {
        if (!prev) return prev;
        const updatedRounds = [...(prev.rounds || [])];
        const existingIdx = updatedRounds.findIndex((r) => r.round_num === roundData.round_num);
        if (existingIdx >= 0) {
          updatedRounds[existingIdx] = roundData;
        } else {
          updatedRounds.push(roundData);
        }
        return { ...prev, rounds: updatedRounds };
      });
    });

    eventSource.addEventListener('session_complete', (e) => {
      const completeData = JSON.parse(e.data);
      setDebate((prev) => (prev ? { ...prev, ...completeData } : prev));
      eventSource.close();
    });

    return () => {
      eventSource.close();
    };
  }, [debateId, apiKey]);

  const toggleRound = (roundNum) => {
    setExpandedRounds((prev) => ({ ...prev, [roundNum]: !prev[roundNum] }));
  };

  // Default values matching UI.png reference mockup
  const displayId = debate ? debate.id.substring(0, 4) : "1024";
  const repoName = debate?.pr_repo || debate?.repo_ref || "acme-inc/upload-service";
  const prNumber = debate?.pr_number ? `#${debate.pr_number}` : "#87";
  const prAuthor = debate?.pr_author || "johndoe";
  const ticketText = debate?.ticket || "Fix unsafe file handling in upload service";
  const targetFile = debate?.target_file || "src/upload.py";
  const status = debate?.status || "running";

  const reviewerCritique = debate?.rounds?.[0]?.reviewer_text ||
    "The proposed patch partially addresses the path traversal, but it still relies on user-controlled filename. This can be bypassed with encoded paths (e.g. %2e%2e/). You should use Path.resolve() and ensure the resolved path is within the upload directory. Also, missing file size validation.";

  const patcherResponse = debate?.rounds?.[0]?.patch_text ||
    "Updated the implementation to resolve the path safely and added file size validation. Also added tests for encoded path traversal.";

  return (
    <div className="max-w-[1240px] mx-auto px-4 py-6">
      {/* Top Navigation & Breadcrumb */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2 text-sm text-gray-500 font-medium">
          <button onClick={onBack} className="hover:text-gray-900 flex items-center gap-1 transition-colors">
            Debates
          </button>
          <span>/</span>
          <span className="text-gray-900 font-semibold">#{displayId}</span>
        </div>
      </div>

      {/* Main Header Title & Badges */}
      <div className="mb-6">
        <div className="flex items-center gap-3 mb-2">
          <h1 className="text-2xl font-bold text-gray-900 tracking-tight">
            #{displayId} | {ticketText}
          </h1>
          <span className="px-3 py-1 rounded-full text-xs font-semibold bg-emerald-100 text-emerald-700 flex items-center gap-1.5 border border-emerald-200">
            <span className="w-2 h-2 rounded-full bg-emerald-600 animate-pulse"></span>
            Running
          </span>
        </div>
        <p className="text-sm text-gray-500 font-mono">
          <span className="text-gray-700 font-medium">{repoName}</span>
          <span className="mx-2">•</span>
          <span>PR {prNumber}</span>
          <span className="mx-2">•</span>
          <span>opened 2 hours ago by {prAuthor}</span>
        </p>
      </div>

      {/* Two Column Layout */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
        {/* Left Main Column (Pipeline + Feed + Collapsed Rounds) */}
        <div className="lg:col-span-8">
          {/* Top Pipeline Stepper Card */}
          <PipelineStepper currentRound={2} totalRounds={5} />

          {/* Active Round Feed */}
          <div className="relative pl-6 space-y-6 mb-8">
            {/* Vertical timeline connector */}
            <div className="absolute top-4 bottom-4 left-[11px] w-[2px] bg-blue-100"></div>

            {/* Reviewer Entry */}
            <div className="relative flex items-start gap-4 bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
              <div className="absolute -left-[31px] top-6 w-6 h-6 rounded-full bg-blue-600 text-white flex items-center justify-center text-xs font-bold ring-4 ring-white shadow-sm">
                R
              </div>
              <div className="w-full">
                <div className="flex items-center justify-between mb-1">
                  <div className="flex items-center gap-2">
                    <span className="font-bold text-gray-900 text-sm">Reviewer</span>
                    <span className="text-xs text-gray-400">2 minutes ago</span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <span className="px-2 py-0.5 rounded text-[11px] font-medium bg-red-100 text-red-700">security</span>
                    <span className="px-2 py-0.5 rounded text-[11px] font-medium bg-blue-100 text-blue-700">correctness</span>
                    <span className="px-2 py-0.5 rounded text-[11px] font-medium bg-amber-100 text-amber-700">missing tests</span>
                  </div>
                </div>
                <p className="text-xs text-gray-400 italic mb-3">Finds issues. Challenges assumptions.</p>
                <div className="text-xs text-gray-700 leading-relaxed font-sans bg-gray-50/50 p-3 rounded-lg border border-gray-100">
                  {reviewerCritique}
                </div>
              </div>
            </div>

            {/* Patcher Entry */}
            <div className="relative flex items-start gap-4 bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
              <div className="absolute -left-[31px] top-6 w-6 h-6 rounded-full bg-emerald-600 text-white flex items-center justify-center text-xs font-bold ring-4 ring-white shadow-sm">
                ✓
              </div>
              <div className="w-full">
                <div className="flex items-center justify-between mb-1">
                  <div className="flex items-center gap-2">
                    <span className="font-bold text-gray-900 text-sm">Patcher</span>
                    <span className="text-xs text-gray-400">Just now</span>
                  </div>
                  <span className="px-2.5 py-0.5 rounded text-[11px] font-medium bg-emerald-100 text-emerald-700">
                    Proposed patch
                  </span>
                </div>
                <p className="text-xs text-gray-400 italic mb-3">Proposes fixes. Iterates based on feedback.</p>
                <p className="text-xs text-gray-700 leading-relaxed font-sans mb-3">
                  {patcherResponse}
                </p>

                {/* Code Diff Component */}
                <DiffViewer patchText={debate?.rounds?.[0]?.patch_text} filename={targetFile} />
              </div>
            </div>
          </div>

          {/* Collapsed Round List */}
          <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm divide-y divide-gray-100">
            {/* Round 1 (Completed) */}
            <div className="p-4 hover:bg-gray-50/50 transition-colors">
              <button
                onClick={() => toggleRound(1)}
                className="w-full flex items-center justify-between text-left"
              >
                <div className="flex items-center gap-3">
                  <div className="w-5 h-5 rounded-full bg-emerald-600 text-white flex items-center justify-center text-xs">
                    <Check className="w-3.5 h-3.5 stroke-[3]" />
                  </div>
                  <span className="font-bold text-gray-900 text-xs">Round 1</span>
                  <span className="text-xs text-gray-500 font-medium">
                    Initial review, identified path traversal vulnerability
                  </span>
                </div>
                <div className="flex items-center gap-3">
                  <span className="text-xs font-mono text-gray-400">1m 02s</span>
                  {expandedRounds[1] ? <ChevronUp className="w-4 h-4 text-gray-400" /> : <ChevronDown className="w-4 h-4 text-gray-400" />}
                </div>
              </button>

              {expandedRounds[1] && (
                <div className="mt-3 pt-3 border-t border-gray-100 text-xs text-gray-600 font-mono">
                  Round 1 details: Gate check failed (3 tests failed). Reviewer requested path sanitization.
                </div>
              )}
            </div>

            {/* Round 3 (Pending) */}
            <div className="p-4 flex items-center justify-between text-gray-400 text-xs">
              <div className="flex items-center gap-3">
                <div className="w-5 h-5 rounded-full border border-gray-300 flex items-center justify-center text-[10px]">◯</div>
                <span className="font-semibold text-gray-700">Round 3</span>
                <span>Pending</span>
              </div>
              <div className="flex items-center gap-3 font-mono text-gray-400">
                <span>Not started</span>
                <ChevronDown className="w-4 h-4 text-gray-300" />
              </div>
            </div>

            {/* Round 4 (Pending) */}
            <div className="p-4 flex items-center justify-between text-gray-400 text-xs">
              <div className="flex items-center gap-3">
                <div className="w-5 h-5 rounded-full border border-gray-300 flex items-center justify-center text-[10px]">◯</div>
                <span className="font-semibold text-gray-700">Round 4</span>
                <span>Pending</span>
              </div>
              <div className="flex items-center gap-3 font-mono text-gray-400">
                <span>Not started</span>
                <ChevronDown className="w-4 h-4 text-gray-300" />
              </div>
            </div>

            {/* Round 5 (Pending) */}
            <div className="p-4 flex items-center justify-between text-gray-400 text-xs">
              <div className="flex items-center gap-3">
                <div className="w-5 h-5 rounded-full border border-gray-300 flex items-center justify-center text-[10px]">◯</div>
                <span className="font-semibold text-gray-700">Round 5</span>
                <span>Pending</span>
              </div>
              <div className="flex items-center gap-3 font-mono text-gray-400">
                <span>Not started</span>
                <ChevronDown className="w-4 h-4 text-gray-300" />
              </div>
            </div>
          </div>
        </div>

        {/* Right Sidebar Column */}
        <div className="lg:col-span-4">
          {/* Card 1: Debate Details */}
          <div className="bg-white border border-gray-200 rounded-xl p-5 mb-5 shadow-sm">
            <h3 className="font-semibold text-gray-900 text-sm mb-4">Debate Details</h3>

            <div className="space-y-3 text-xs">
              <div className="flex justify-between py-1 border-b border-gray-50">
                <span className="text-gray-500 font-medium">Status</span>
                <span className="text-emerald-600 font-semibold flex items-center gap-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-600"></span>
                  Running
                </span>
              </div>
              <div className="flex justify-between py-1 border-b border-gray-50">
                <span className="text-gray-500 font-medium">Current round</span>
                <span className="font-mono font-semibold text-gray-900">2 / 5</span>
              </div>
              <div className="flex justify-between py-1 border-b border-gray-50">
                <span className="text-gray-500 font-medium">Started at</span>
                <span className="font-mono text-gray-700">Sep 23, 2026 14:32</span>
              </div>
              <div className="flex justify-between py-1 border-b border-gray-50">
                <span className="text-gray-500 font-medium">Duration</span>
                <span className="font-mono text-gray-700">4m 18s</span>
              </div>
              <div className="flex justify-between py-1 border-b border-gray-50">
                <span className="text-gray-500 font-medium">Repository</span>
                <a href="#" className="text-blue-600 font-mono hover:underline flex items-center gap-1">
                  {repoName}
                </a>
              </div>
              <div className="flex justify-between py-1 border-b border-gray-50">
                <span className="text-gray-500 font-medium">PR</span>
                <a href="#" className="text-blue-600 font-mono hover:underline">
                  {prNumber}
                </a>
              </div>
              <div className="flex justify-between py-1 border-b border-gray-50">
                <span className="text-gray-500 font-medium">Author</span>
                <span className="font-mono text-gray-700">{prAuthor}</span>
              </div>
              <div className="flex justify-between py-1 border-b border-gray-50">
                <span className="text-gray-500 font-medium">Base branch</span>
                <span className="font-mono text-gray-700">main</span>
              </div>
              <div className="flex justify-between py-1 border-b border-gray-50">
                <span className="text-gray-500 font-medium">Head branch</span>
                <span className="font-mono text-gray-700">fix/upload-validation</span>
              </div>
              <div className="flex justify-between py-1">
                <span className="text-gray-500 font-medium">Labels</span>
                <div className="flex items-center gap-1">
                  <span className="px-2 py-0.5 rounded text-[10px] font-medium bg-red-100 text-red-700">security</span>
                  <span className="px-2 py-0.5 rounded text-[10px] font-medium bg-blue-100 text-blue-700">bug</span>
                </div>
              </div>
            </div>
          </div>

          {/* Card 2: Gate Results */}
          <GateChecklist roundNum={1} gateResult={debate?.rounds?.[0]?.gate_result} />

          {/* Card 3: Activity Timeline */}
          <ActivityTimeline />
        </div>
      </div>
    </div>
  );
}
