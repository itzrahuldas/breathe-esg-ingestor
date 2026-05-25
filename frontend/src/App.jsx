// src/App.jsx
import { BrowserRouter, Route, Routes, Navigate } from 'react-router-dom';
import Navbar from './components/Navbar';
import { ToastContainer } from './components/Toast';
import Dashboard from './pages/Dashboard';
import Upload    from './pages/Upload';
import Review    from './pages/Review';
import AuditLog  from './pages/AuditLog';

export default function App() {
  return (
    <BrowserRouter>
      <Navbar />
      <Routes>
        <Route path="/"       element={<Dashboard />} />
        <Route path="/upload" element={<Upload />} />
        <Route path="/review" element={<Review />} />
        <Route path="/audit"  element={<AuditLog />} />
        <Route path="*"       element={<Navigate to="/" replace />} />
      </Routes>
      <ToastContainer />
    </BrowserRouter>
  );
}
