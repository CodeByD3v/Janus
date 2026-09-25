import React from 'react';
import { Check, Clock, Play, AlertTriangle } from 'lucide-react';

export default function PipelineStepper({ currentRound = 2, totalRounds = 5, activeStep = 'patcher', rounds = [] }) {
  // Steps within a round: Reviewer -> Patcher -> Gate
  const pipelineSteps = [
    { id: 'reviewer', label: 'Reviewer', subtext: 'Completed', duration: '1m 12s' },
    { id: 'patcher', label: 'Patcher', subtext: 'In progress', duration: '23s' },
    { id: 'gate', label: 'Gate', subtext: 'Pending', duration: '' },
    { id: 'round3', label: 'Round 3', subtext: 'Pending', duration: '' },
    { id: 'round4', label: 'Round 4', subtext: 'Pending', duration: '' },
    { id: 'round5', label: 'Round 5', subtext: 'Pending', duration: '' },
  ];

  const getStepState = (index) => {
    if (index === 0) return 'completed';
    if (index === 1) return 'active';
    return 'pending';
  };

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 mb-6 shadow-sm">
      {/* Round Counter Title */}
      <h2 className="text-base font-semibold text-gray-900 mb-6">
        Round {currentRound} of {totalRounds}
      </h2>

      {/* Horizontal Stepper */}
      <div className="relative flex items-center justify-between px-4">
        {pipelineSteps.map((step, idx) => {
          const state = getStepState(idx);
          const isLast = idx === pipelineSteps.length - 1;

          return (
            <React.Fragment key={step.id}>
              {/* Node */}
              <div className="flex flex-col items-center z-10 relative">
                {/* Node Ring & Icon */}
                <div className="relative">
                  {state === 'completed' && (
                    <div className="w-9 h-9 rounded-full bg-emerald-600 text-white flex items-center justify-center shadow-sm">
                      <Check className="w-5 h-5 stroke-[2.5]" />
                    </div>
                  )}

                  {state === 'active' && (
                    <div className="w-9 h-9 rounded-full bg-blue-600 text-white flex items-center justify-center animate-pulse-ring shadow-md ring-4 ring-blue-100">
                      <div className="w-3.5 h-3.5 rounded-full bg-white"></div>
                    </div>
                  )}

                  {state === 'pending' && idx === 2 && (
                    <div className="w-9 h-9 rounded-full bg-gray-100 border-2 border-gray-300 text-gray-400 flex items-center justify-center">
                      <Clock className="w-4 h-4 text-gray-500" />
                    </div>
                  )}

                  {state === 'pending' && idx > 2 && (
                    <div className="w-9 h-9 rounded-full bg-gray-50 border-2 border-gray-300 text-gray-400 flex items-center justify-center">
                      <div className="w-2.5 h-2.5 rounded-full bg-gray-300"></div>
                    </div>
                  )}
                </div>

                {/* Node Label */}
                <span className={`mt-2 text-xs font-semibold ${
                  state === 'completed' ? 'text-emerald-700' :
                  state === 'active' ? 'text-blue-600 font-bold' : 'text-gray-500'
                }`}>
                  {step.label}
                </span>

                {/* Node Subtext */}
                <span className="text-[11px] text-gray-400 mt-0.5 font-normal">
                  {state === 'completed' ? 'Completed' : state === 'active' ? 'In progress' : 'Pending'}
                </span>

                {/* Duration */}
                {step.duration && (
                  <span className="text-[10px] text-gray-400 font-mono mt-0.5">
                    {step.duration}
                  </span>
                )}
              </div>

              {/* Connecting Line */}
              {!isLast && (
                <div className="flex-1 h-[2px] mx-2 -mt-7 z-0 bg-gray-200 relative">
                  <div
                    className={`h-full transition-all duration-300 ${
                      idx === 0 ? 'bg-emerald-600 w-full' :
                      idx === 1 ? 'bg-gradient-to-r from-blue-600 to-gray-200 w-1/2' : 'w-0'
                    }`}
                  ></div>
                </div>
              )}
            </React.Fragment>
          );
        })}
      </div>
    </div>
  );
}
