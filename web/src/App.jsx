import React, { useState } from 'react';
import Navbar from './components/Navbar';
import CustomerDashboard from './components/CustomerDashboard';
import AdminConsole from './components/AdminConsole';
import LiveDebateView from './components/LiveDebateView';

export default function App() {
  const [activeSurface, setActiveSurface] = useState('customer'); // 'customer' | 'admin'
  const [selectedDebateId, setSelectedDebateId] = useState(null);
  const [apiKey, setApiKey] = useState('test-key');

  return (
    <div className="min-h-screen bg-[#f6f7f9] text-[#111827]">
      <Navbar
        activeSurface={activeSurface}
        setActiveSurface={(surface) => {
          setActiveSurface(surface);
          setSelectedDebateId(null);
        }}
        apiKey={apiKey}
        setApiKey={setApiKey}
      />

      <main>
        {selectedDebateId ? (
          <LiveDebateView
            debateId={selectedDebateId}
            apiKey={apiKey}
            onBack={() => setSelectedDebateId(null)}
          />
        ) : activeSurface === 'customer' ? (
          <CustomerDashboard
            apiKey={apiKey}
            onSelectDebate={(id) => setSelectedDebateId(id)}
          />
        ) : (
          <AdminConsole
            apiKey={apiKey}
            onSelectDebate={(id) => setSelectedDebateId(id)}
          />
        )}
      </main>
    </div>
  );
}
