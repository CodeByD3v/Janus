import React from 'react';
import { CheckCircle2, Clock } from 'lucide-react';

export default function ActivityTimeline({ events = [] }) {
  const defaultEvents = [
    { title: 'Round 2 started', subtitle: '', time: 'Just now', type: 'active' },
    { title: 'Round 1 gate completed', subtitle: 'Tests failed', time: '2m ago', type: 'completed' },
    { title: 'Patcher submitted changes', subtitle: '', time: '2m ago', type: 'completed' },
    { title: 'Reviewer completed review', subtitle: '', time: '3m ago', type: 'completed' },
    { title: 'Debate started', subtitle: '', time: '4m ago', type: 'start' },
  ];

  const items = events.length > 0 ? events : defaultEvents;

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
      <h3 className="font-semibold text-gray-900 text-sm mb-4">Activity Timeline</h3>

      <div className="relative pl-6 space-y-4">
        {/* Continuous vertical line behind icons */}
        <div className="absolute top-2 bottom-2 left-[9px] w-[2px] bg-gray-200"></div>

        {items.map((item, idx) => (
          <div key={idx} className="relative flex items-start justify-between text-xs">
            {/* Dot/Icon */}
            <div className="absolute -left-6 top-0.5 z-10 bg-white">
              {item.type === 'active' && (
                <div className="w-4 h-4 rounded-full bg-blue-600 flex items-center justify-center animate-pulse-ring">
                  <div className="w-1.5 h-1.5 rounded-full bg-white"></div>
                </div>
              )}
              {item.type === 'completed' && (
                <CheckCircle2 className="w-4.5 h-4.5 text-emerald-600 bg-white" />
              )}
              {item.type === 'start' && (
                <div className="w-4 h-4 rounded-full bg-gray-300 flex items-center justify-center">
                  <div className="w-1.5 h-1.5 rounded-full bg-white"></div>
                </div>
              )}
            </div>

            {/* Content */}
            <div className="pr-2">
              <span className={`font-medium ${item.type === 'active' ? 'text-blue-600 font-semibold' : 'text-gray-900'}`}>
                {item.title}
              </span>
              {item.subtitle && (
                <p className="text-[11px] text-gray-500 mt-0.5">{item.subtitle}</p>
              )}
            </div>

            {/* Time */}
            <span className="text-[11px] text-gray-400 font-normal shrink-0">
              {item.time}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
