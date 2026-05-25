// src/components/Toast.jsx
import { useEffect, useState } from 'react';

let _addToast = null;

export function toast(msg, type = 'info') {
  _addToast?.({ msg, type, id: Date.now() });
}

export function ToastContainer() {
  const [toasts, setToasts] = useState([]);

  useEffect(() => {
    _addToast = (t) => {
      setToasts(prev => [...prev, t]);
      setTimeout(() => setToasts(prev => prev.filter(x => x.id !== t.id)), 5000);
    };
    return () => { _addToast = null; };
  }, []);

  const dismiss = (id) => setToasts(prev => prev.filter(x => x.id !== id));

  return (
    <div className="toast-container">
      {toasts.map(t => (
        <div key={t.id} className={`toast ${t.type}`}>
          <span>{t.msg}</span>
          <span className="toast-close" onClick={() => dismiss(t.id)}>✕</span>
        </div>
      ))}
    </div>
  );
}
