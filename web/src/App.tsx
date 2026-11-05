import { NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { ConfirmHost, ConnectionBanner, Toasts } from './components/ui';
import { useAppBootstrap, useAppState } from './state/app';
import Dashboard from './pages/Dashboard';
import SetsPage from './pages/Sets';
import MediaPage from './pages/Media';
import SettingsPage from './pages/Settings';
import Editor from './editor/Editor';

const NAV = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/sets', label: 'Sets' },
  { to: '/media', label: 'Media' },
  { to: '/settings', label: 'Settings' },
];

export default function App() {
  useAppBootstrap();
  const state = useAppState();
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand"><span className="dots"><i /><i /><i /></span>TrafficLight</div>
        <nav>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => (isActive ? 'active' : '')}>{n.label}</NavLink>
          ))}
        </nav>
        <div className="version">{state ? `v${state.version}` : ''}</div>
      </header>
      <ConnectionBanner />
      <main>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/sets" element={<SetsPage />} />
          <Route path="/editor/:id" element={<Editor />} />
          <Route path="/media" element={<MediaPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <Toasts />
      <ConfirmHost />
    </div>
  );
}
