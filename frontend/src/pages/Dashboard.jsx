// src/pages/Dashboard.jsx
import { useEffect, useState } from 'react';
import { api } from '../api';

function MetricCard({ label, value, color }) {
  return (
    <div className={`metric-card ${color || ''}`}>
      <div className="metric-label">{label}</div>
      <div className="metric-value">{value ?? '—'}</div>
    </div>
  );
}

function ScopeBar({ breakdown, total }) {
  if (!total) return null;
  const pct = (v) => total ? ((v / total) * 100).toFixed(1) : 0;
  const fmt = (v) => v >= 1000 ? `${(v/1000).toFixed(1)}t` : `${v.toFixed(0)}kg`;

  return (
    <div className="scope-bar-wrap">
      <div className="scope-bar-title">Scope Breakdown — kgCO₂e</div>
      <div className="scope-bar-track">
        {[1,2,3].map(s => {
          const val = breakdown?.[s] ?? 0;
          const w = pct(val);
          return (
            <div
              key={s} className={`scope-bar-seg s${s}`}
              style={{ width: `${w}%` }}
              title={`Scope ${s}: ${val.toFixed(2)} kgCO₂e`}
            >
              {w > 8 ? `${w}%` : ''}
            </div>
          );
        })}
      </div>
      <div className="scope-bar-legend">
        {[
          { s:1, label:'Scope 1 — Direct combustion' },
          { s:2, label:'Scope 2 — Purchased electricity' },
          { s:3, label:'Scope 3 — Business travel / goods' },
        ].map(({ s, label }) => (
          <div key={s} className="scope-legend-item">
            <span className={`scope-legend-dot s${s}`} />
            <span style={{ color:'var(--text-secondary)' }}>{label}</span>
            <span style={{ fontWeight:600 }}>
              {fmt(breakdown?.[s] ?? 0)} ({pct(breakdown?.[s] ?? 0)}%)
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    api.summary()
      .then(setData)
      .catch(e => setErr(e.message));
  }, []);

  const fmt = (n) => n != null ? Number(n).toLocaleString('en-IN', { maximumFractionDigits: 1 }) : '—';
  const breakdown = data ? { 1: data.scope_breakdown?.['1'] ?? 0, 2: data.scope_breakdown?.['2'] ?? 0, 3: data.scope_breakdown?.['3'] ?? 0 } : {};

  return (
    <main className="page">
      <h1 className="page-title">Dashboard</h1>

      {err && <div className="toast error" style={{ marginBottom:20, maxWidth:'none' }}>{err}</div>}

      <div className="metrics-grid">
        <MetricCard label="Total Rows"    value={fmt(data?.total_rows)}      />
        <MetricCard label="Flagged"       value={fmt(data?.flagged)}          color="amber" />
        <MetricCard label="Pending Review" value={fmt(data?.pending_review)} color="blue" />
        <MetricCard label="Approved"      value={fmt(data?.approved)}         color="green" />
        <MetricCard label="Total kgCO₂e"  value={fmt(data?.total_kgco2e)}    color="purple" />
      </div>

      <ScopeBar breakdown={breakdown} total={data?.total_kgco2e ?? 0} />

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:16 }}>
        <div className="card">
          <div style={{ fontSize:'.8rem', fontWeight:600, textTransform:'uppercase', letterSpacing:'.07em', color:'var(--text-secondary)', marginBottom:14 }}>Status Overview</div>
          {[
            { label:'Pending Review', val: data?.pending_review ?? 0, color:'var(--blue-lt)' },
            { label:'Flagged',        val: data?.flagged ?? 0,        color:'var(--amber-lt)' },
            { label:'Approved / Locked', val: data?.approved ?? 0,   color:'var(--green-lt)' },
            { label:'Rejected',       val: data?.rejected ?? 0,       color:'var(--red-lt)' },
          ].map(({ label, val, color }) => (
            <div key={label} style={{ display:'flex', justifyContent:'space-between', alignItems:'center', padding:'8px 0', borderBottom:'1px solid var(--border-subtle)' }}>
              <span style={{ fontSize:'.85rem', color:'var(--text-secondary)' }}>{label}</span>
              <span style={{ fontWeight:700, color }}>{fmt(val)}</span>
            </div>
          ))}
        </div>
        <div className="card" style={{ display:'flex', flexDirection:'column', justifyContent:'center', alignItems:'center', gap:8, minHeight:160 }}>
          <div style={{ fontSize:'3rem', fontWeight:700, letterSpacing:'-.04em', color:'var(--green-lt)' }}>
            {fmt(data?.total_kgco2e)}
          </div>
          <div style={{ fontSize:'.85rem', color:'var(--text-secondary)' }}>kgCO₂e total emissions</div>
          <div style={{ fontSize:'.75rem', color:'var(--text-muted)' }}>across all scopes, all sources</div>
        </div>
      </div>
    </main>
  );
}
