import React, { useState } from 'react';
import { Copy, Check, Code } from 'lucide-react';

export default function DiffViewer({ patchText, filename = "src/upload.py", language = "Python" }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    if (patchText) {
      navigator.clipboard.writeText(patchText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  // Mock or parse sample diff lines if patchText is raw unified diff
  const parseDiff = (text) => {
    if (!text || text.trim() === '') {
      return {
        additions: 14,
        deletions: 6,
        lines: [
          { type: 'normal', oldNo: 1, newNo: 1, content: 'import os' },
          { type: 'delete', oldNo: 2, newNo: null, content: '- import os' },
          { type: 'add', oldNo: null, newNo: 2, content: '+ from pathlib import Path' },
          { type: 'delete', oldNo: 3, newNo: null, content: '- def save_file(file, filename):' },
          { type: 'delete', oldNo: 4, newNo: null, content: '-     file_path = os.path.join(UPLOAD_DIR, filename)' },
          { type: 'delete', oldNo: 5, newNo: null, content: '-     with open(file_path, \'wb\') as f:' },
          { type: 'delete', oldNo: 6, newNo: null, content: '-         f.write(file.read())' },
          { type: 'delete', oldNo: 7, newNo: null, content: '-     return file_path' },
          { type: 'add', oldNo: null, newNo: 3, content: '+ MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB' },
          { type: 'add', oldNo: null, newNo: 4, content: '+ UPLOAD_DIR = Path(UPLOAD_DIR).resolve()' },
          { type: 'add', oldNo: null, newNo: 5, content: '+' },
          { type: 'add', oldNo: null, newNo: 6, content: '+ def save_file(file, filename):' },
          { type: 'add', oldNo: null, newNo: 7, content: '+     if file.size > MAX_FILE_SIZE:' },
          { type: 'add', oldNo: null, newNo: 8, content: '+         raise ValueError("File too large")' },
          { type: 'add', oldNo: null, newNo: 9, content: '+     file_path = (UPLOAD_DIR / filename).resolve()' },
          { type: 'add', oldNo: null, newNo: 10, content: '+     if not str(file_path).startswith(str(UPLOAD_DIR)):' },
          { type: 'add', oldNo: null, newNo: 11, content: '+         raise ValueError("Invalid file path")' },
          { type: 'add', oldNo: null, newNo: 12, content: '+     with open(file_path, \'wb\') as f:' },
          { type: 'add', oldNo: null, newNo: 13, content: '+         f.write(file.read())' },
          { type: 'add', oldNo: null, newNo: 14, content: '+     return str(file_path)' },
        ]
      };
    }

    const rawLines = text.split('\n');
    let oldNo = 1;
    let newNo = 1;
    let additions = 0;
    let deletions = 0;

    const parsedLines = rawLines.map((line) => {
      if (line.startsWith('+') && !line.startsWith('+++')) {
        additions++;
        return { type: 'add', oldNo: null, newNo: newNo++, content: line };
      } else if (line.startsWith('-') && !line.startsWith('---')) {
        deletions++;
        return { type: 'delete', oldNo: oldNo++, newNo: null, content: line };
      } else if (line.startsWith('@@')) {
        return { type: 'hunk', oldNo: null, newNo: null, content: line };
      } else {
        return { type: 'normal', oldNo: oldNo++, newNo: newNo++, content: line };
      }
    });

    return { additions, deletions, lines: parsedLines };
  };

  const diffData = parseDiff(patchText);

  // Syntax highlighting helper for code lines
  const highlightSyntax = (content, type) => {
    let text = content;
    if (type === 'add' || type === 'delete') {
      text = text.substring(1); // remove + or - prefix for highlighting
    }

    // Basic keyword replacement for python/ts
    const keywords = ['import', 'from', 'def', 'class', 'return', 'if', 'else', 'raise', 'try', 'except', 'with', 'as', 'not', 'and', 'or', 'const', 'let', 'function'];
    const parts = text.split(/(\s+|[(),:]|"[^"]*"|'[^']*'|#[^\n]*)/);

    return (
      <span>
        {parts.map((part, idx) => {
          if (keywords.includes(part.trim())) {
            return <span key={idx} className="text-blue-600 font-semibold">{part}</span>;
          } else if (part.startsWith('"') || part.startsWith("'")) {
            return <span key={idx} className="text-emerald-700">{part}</span>;
          } else if (part.startsWith('#')) {
            return <span key={idx} className="text-gray-400 italic">{part}</span>;
          } else if (!isNaN(Number(part.trim())) && part.trim() !== '') {
            return <span key={idx} className="text-amber-600">{part}</span>;
          }
          return <span key={idx}>{part}</span>;
        })}
      </span>
    );
  };

  return (
    <div className="mt-3 border border-gray-200 rounded-lg overflow-hidden bg-white text-xs font-mono shadow-sm">
      {/* Header Bar */}
      <div className="flex items-center justify-between px-3 py-2 bg-gray-50 border-b border-gray-200 text-gray-700 font-sans">
        <div className="flex items-center gap-2">
          <span className="font-semibold font-mono text-gray-900">{filename}</span>
          <span className="px-1.5 py-0.5 rounded text-[11px] font-mono bg-red-100 text-red-700 font-medium">-{diffData.deletions}</span>
          <span className="px-1.5 py-0.5 rounded text-[11px] font-mono bg-green-100 text-green-700 font-medium">+{diffData.additions}</span>
        </div>
        <div className="flex items-center gap-3">
          <span className="px-2 py-0.5 rounded text-[11px] bg-gray-200 text-gray-700">{language}</span>
          <button
            onClick={handleCopy}
            className="p-1 text-gray-500 hover:text-gray-800 rounded transition-colors"
            title="Copy diff"
          >
            {copied ? <Check className="w-3.5 h-3.5 text-green-600" /> : <Copy className="w-3.5 h-3.5" />}
          </button>
        </div>
      </div>

      {/* Code Diff Table */}
      <div className="overflow-x-auto max-h-[380px]">
        <table className="w-full border-collapse font-mono text-[12px] leading-5">
          <tbody>
            {diffData.lines.map((line, idx) => {
              if (line.type === 'hunk') {
                return (
                  <tr key={idx} className="bg-blue-50/50 text-blue-600 border-y border-blue-100">
                    <td colSpan={2} className="px-2 py-1 text-right text-blue-400 select-none border-r border-blue-100">@@</td>
                    <td className="px-3 py-1 italic font-medium">{line.content}</td>
                  </tr>
                );
              }

              const isDelete = line.type === 'delete';
              const isAdd = line.type === 'add';

              return (
                <tr
                  key={idx}
                  className={`hover:bg-opacity-80 transition-colors ${
                    isDelete ? 'bg-red-50/70 text-red-900' : isAdd ? 'bg-green-50/70 text-green-900' : 'bg-white text-gray-800'
                  }`}
                >
                  {/* Old line number */}
                  <td className={`w-10 px-2 py-0.5 text-right select-none text-gray-400 border-r border-gray-100 ${isDelete ? 'bg-red-100/50 text-red-600 font-medium' : ''}`}>
                    {line.oldNo || ''}
                  </td>
                  {/* New line number */}
                  <td className={`w-10 px-2 py-0.5 text-right select-none text-gray-400 border-r border-gray-100 ${isAdd ? 'bg-green-100/50 text-green-600 font-medium' : ''}`}>
                    {line.newNo || ''}
                  </td>
                  {/* Line prefix (+ or -) */}
                  <td className="w-4 py-0.5 text-center select-none font-bold">
                    {isDelete ? '-' : isAdd ? '+' : ' '}
                  </td>
                  {/* Code Content */}
                  <td className="px-2 py-0.5 whitespace-pre">
                    {highlightSyntax(line.content, line.type)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
