// src/pages/AuditLog.jsx
import { useEffect, useState, useCallback } from 'react';
import { api } from '../api';
import { toast } from '../components/Toast';

function timeAgo(ts) {
  const diff = (Date.now() - new Date(ts)) / 1000;
  if (diff < 60)    return 'just now';
  if (diff < 3600)  return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  if (diff < 604800) return `${Math.floor(diff/86400)}d ago`;
  return new Date(ts).toLocaleDateString('en-GB', { day:'2-digit', month:'short', year:'numeric' });
}

const ACTION_META = {
  UPLOADED: { cls:'uploaded', icon:'⬆', label:'Uploaded' },
  APPROVED: { cls:'approved', icon:'✅', label:'Approved & Locked' },
  REJECTED: { cls:'rejected', icon:'✖', label:'Rejected' },
  FLAGGED:  { cls:'flagged',  icon:'⚠', label:'Flagged' },
  LOCKED:   { cls:'approved', icon:'🔒', label:'Locked' },
  REVIEWED: { cls:'pending',  icon:'👁', label:'Reviewed' },
};

export default function AuditLog() {
  const [entries, setEntries] = useState([]);
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [rowFilter, setRowFilter] = useState('');

  const fetch = useCallback(async () => {
    setLoading(true);
    try {
      const params = {};
      if (rowFilter.trim()) params.row_id = rowFilter.trim();
      const data = await api.auditLog(params);
      setEntries(data.results ?? []);
      setCount(data.count ?? 0);
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      setLoading(false);
    }
  }, [rowFilter]);

  useEffect(() => { fetch(); }, [fetch]);

  return (
    <main className="page">
      <h1 className="page-title">Audit Log <span style={{ fontSize:'1rem', fontWeight:400, color:'var(--text-secondary)' }}>({count} entries)</span></h1>

      <div className="filter-bar" style={{ marginBottom:28 }}>
        <input
          className="filter-input" placeholder="Filter by Row ID…"
          value={rowFilter} onChange={e => setRowFilter(e.target.value)}
          style={{ width:180 }}
        />
        <button className="btn btn-ghost" onClick={fetch}>Refresh</button>
      </div>

      {loading && <div style={{ textAlign:'center', padding:40, color:'var(--text-muted)' }}><span className="spinner" /></div>}

      {!loading && entries.length === 0 && (
        <div className="empty-state">
          <div className="empty-icon">📋</div>
          <p>No audit entries found.</p>
        </div>
      )}

      {!loading && entries.length > 0 && (
        <div className="audit-list">
          {entries.map(entry => {
            const meta = ACTION_META[entry.action] ?? { cls:'pending', icon:'•', label: entry.action };
            const hasDiff = entry.before_value || entry.after_value;
            return (
              <div key={entry.id} className={`audit-entry action-${entry.action.toLowerCase()}`}>
                <div className="audit-meta">
                  <span className="audit-time">{timeAgo(entry.timestamp)}</span>
                  <span className="audit-actor">Actor #{entry.actor_id || 'system'}</span>
                  <span className={`badge ${meta.cls}`} style={{ width:'fit-content', marginTop:4 }}>
                    {meta.icon} {meta.label}
                  </span>
                </div>
                <div className="audit-body">
                  <div className="audit-row-id">Row #{entry.activity_row_id}</div>
                  {entry.detail && (
                    <div className="audit-detail">{entry.detail}</div>
                  )}
                  {hasDiff && (
                    <div className="diff-block">
                      {entry.before_value && Object.entries(entry.before_value).map(([k,v]) => (
                        <span key={k} className="diff-pill before">− {k}: {String(v)}</span>
                      ))}
                      {entry.after_value && Object.entries(entry.after_value).map(([k,v]) => (
                        <span key={k} className="diff-pill after">+ {k}: {String(v)}</span>
                      ))}
                    </div>
                  )}
                  <div style={{ fontSize:'.72rem', color:'var(--text-muted)', marginTop:6 }}>
                    {new Date(entry.timestamp).toLocaleString('en-GB')}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </main>
  );
}
