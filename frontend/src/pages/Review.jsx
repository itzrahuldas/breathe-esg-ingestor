// src/pages/Review.jsx
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { toast } from '../components/Toast';
import RejectModal from '../components/RejectModal';

// ── Human-readable flag reasons ──────────────────────────────────────────────
function humaniseFlag(raw) {
  if (!raw) return '';
  return raw
    .replace(/Plant code '([^']+)' not found[^.]*\./gi,
      "Plant code '$1' is not in the reference table. Assign a plant name before approving.")
    .replace(/Quantity (-?\d+\.?\d*) is zero or negative[^.]*\./gi,
      "Quantity $1 is negative — this may be a reversal or return. Please verify before approving.")
    .replace(/Zero or negative consumption\./gi,
      'Consumption is zero or negative. Check the original bill.')
    .replace(/Estimated meter reading[^.]*\./gi,
      'This is an estimated meter reading. Verify against the actual bill before approving.')
    .replace(/Billing period exceeds (\d+) days \((\d+) days\)[^.]*\./gi,
      'Billing period of $2 days exceeds the $1-day threshold — likely an estimated read.')
    .replace(/Unknown IATA code: '([^']+)' or '([^']+)'\./gi,
      "Airport code '$1' or '$2' is not in our lookup table. Add it or correct the booking.")
    .replace(/Unknown SAP unit: '([^']+)'\./gi,
      "Unit '$1' is not recognised. Contact your data team to add this unit.")
    .replace(/Unknown utility unit: '([^']+)'\./gi,
      "Unit '$1' is not recognised. Expected kWh or MWh.")
    .replace(/Unknown cabin class '([^']+)'\./gi,
      "Cabin class '$1' is unrecognised. Expected Economy, Business, or First.")
    .replace(/Unknown transport_mode '([^']+)'\./gi,
      "Transport mode '$1' is unrecognised. Expected TAXI, CAR, or RAIL.")
    .replace(/Zero or missing hotel nights\./gi,
      'Hotel nights not provided or zero. Check the original booking confirmation.')
    .replace(/Zero or missing ground distance/gi,
      'Ground distance is missing or zero. Add the trip distance in km.');
}

// ── Helpers ──────────────────────────────────────────────────────────────────
const fmtDate = (d) => d ? new Date(d).toLocaleDateString('en-GB', { day:'2-digit', month:'short', year:'2-digit' }) : '—';
const fmtNum  = (n) => n != null ? Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—';

const SOURCE_MAP = { sap_csv:'SAP', utility_csv:'Utility', travel_csv:'Travel' };

function StatusBadge({ status, isFlagged }) {
  const s = status?.toLowerCase();
  const cls = s === 'pending' && isFlagged ? 'flagged' : s;
  const icons = { pending:'⏳', flagged:'⚠️', approved:'✅', locked:'🔒', rejected:'✖' };
  return <span className={`badge ${cls}`}>{icons[cls] || ''} {status}</span>;
}

function SrcBadge({ src }) {
  const k = src === 'sap_csv' ? 'src-sap' : src === 'utility_csv' ? 'src-util' : 'src-travel';
  return <span className={`badge ${k}`}>{SOURCE_MAP[src] || src}</span>;
}

function ScopeBadge({ scope }) {
  return <span className={`badge scope-${scope}`}>Scope {scope}</span>;
}

export default function Review() {
  const [rows, setRows] = useState([]);
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [rejectModal, setRejectModal] = useState(null); // rowId

  const [filters, setFilters] = useState({
    source_type: '', scope: '', status: '', is_flagged: '',
    date_from: '', date_to: '',
  });

  const fetchRows = useCallback(async () => {
    setLoading(true);
    try {
      const params = {};
      if (filters.source_type) params.source_type = filters.source_type;
      if (filters.scope)       params.scope = filters.scope;
      if (filters.status)      params.status = filters.status;
      if (filters.is_flagged)  params.is_flagged = filters.is_flagged;
      if (filters.date_from)   params.date_from = filters.date_from;
      if (filters.date_to)     params.date_to = filters.date_to;
      const data = await api.rows(params);
      setRows(data.results ?? []);
      setCount(data.count ?? 0);
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => { fetchRows(); }, [fetchRows]);

  const setFilter = (k, v) => setFilters(p => ({ ...p, [k]: v }));

  const handleApprove = async (id) => {
    try {
      await api.approve(id);
      toast('Row approved and locked.', 'success');
      fetchRows();
    } catch (e) { toast(e.message, 'error'); }
  };

  const handleReject = async (id, reason) => {
    try {
      await api.reject(id, reason);
      toast('Row rejected.', 'info');
      setRejectModal(null);
      fetchRows();
    } catch (e) { toast(e.message, 'error'); }
  };

  const handleBulkApprove = async () => {
    const eligible = rows.filter(
      r => !['LOCKED','REJECTED'].includes(r.status)
    ).map(r => r.id);
    if (!eligible.length) { toast('No eligible rows to approve.', 'info'); return; }
    try {
      const res = await api.bulkApprove(eligible);
      toast(`✅ ${res.approved} approved, ${res.skipped} skipped.`, 'success');
      fetchRows();
    } catch (e) { toast(e.message, 'error'); }
  };

  const rowClass = (r) => {
    if (r.status === 'LOCKED' || r.status === 'APPROVED') return 'row-locked';
    if (r.status === 'REJECTED') return 'row-rejected';
    if (r.is_flagged) return 'row-flagged';
    return '';
  };

  return (
    <main className="page">
      <h1 className="page-title">Review Activity Rows <span style={{ fontSize:'1rem', fontWeight:400, color:'var(--text-secondary)' }}>({count} total)</span></h1>

      {/* Filter bar */}
      <div className="filter-bar">
        <select className="filter-select" value={filters.source_type} onChange={e => setFilter('source_type', e.target.value)}>
          <option value="">All Sources</option>
          <option value="SAP">SAP</option>
          <option value="UTILITY">Utility</option>
          <option value="TRAVEL">Travel</option>
        </select>
        <select className="filter-select" value={filters.scope} onChange={e => setFilter('scope', e.target.value)}>
          <option value="">All Scopes</option>
          <option value="1">Scope 1</option>
          <option value="2">Scope 2</option>
          <option value="3">Scope 3</option>
        </select>
        <select className="filter-select" value={filters.status} onChange={e => setFilter('status', e.target.value)}>
          <option value="">All Status</option>
          <option value="PENDING">Pending</option>
          <option value="FLAGGED">Flagged</option>
          <option value="APPROVED">Approved</option>
          <option value="LOCKED">Locked</option>
          <option value="REJECTED">Rejected</option>
        </select>
        <input className="filter-input" type="date" value={filters.date_from} onChange={e => setFilter('date_from', e.target.value)} title="From date" />
        <input className="filter-input" type="date" value={filters.date_to}   onChange={e => setFilter('date_to', e.target.value)}   title="To date" />
        <span className="filter-spacer" />
        <button className="btn btn-green" onClick={handleBulkApprove} disabled={loading}>
          ✅ Approve All Filtered
        </button>
      </div>

      {loading && <div style={{ textAlign:'center', padding:'40px', color:'var(--text-muted)' }}><span className="spinner" /></div>}

      {!loading && rows.length === 0 && (
        <div className="empty-state">
          <div className="empty-icon">📭</div>
          <p>No rows match the current filters.</p>
        </div>
      )}

      {!loading && rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Source</th><th>Scope</th><th>Category</th>
                <th>Site / Route</th><th>Date Range</th>
                <th>Quantity</th><th>Unit</th><th>kgCO₂e</th>
                <th>Status</th><th>Flag Reason</th><th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.id} className={rowClass(r)}>
                  <td><SrcBadge src={r.source_type} /></td>
                  <td><ScopeBadge scope={r.scope} /></td>
                  <td style={{ fontSize:'.78rem', color:'var(--text-secondary)', maxWidth:120 }}>{r.category?.replace(/_/g,' ')}</td>
                  <td style={{ maxWidth:160, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{r.site_name || '—'}</td>
                  <td style={{ whiteSpace:'nowrap', fontSize:'.78rem' }}>
                    {fmtDate(r.activity_date_start)}
                    {r.activity_date_end && r.activity_date_end !== r.activity_date_start ? ` → ${fmtDate(r.activity_date_end)}` : ''}
                  </td>
                  <td style={{ textAlign:'right', fontVariantNumeric:'tabular-nums' }}>{fmtNum(r.quantity)}</td>
                  <td style={{ color:'var(--text-secondary)', fontSize:'.78rem' }}>{r.unit}</td>
                  <td style={{ textAlign:'right', fontVariantNumeric:'tabular-nums', fontWeight:600 }}>{fmtNum(r.kgco2e)}</td>
                  <td><StatusBadge status={r.status} isFlagged={r.is_flagged} /></td>
                  <td><span className="flag-chip">{humaniseFlag(r.flag_reason)}</span></td>
                  <td>
                    {!['LOCKED','REJECTED'].includes(r.status) ? (
                      <div className="row-actions">
                        <button className="btn btn-green btn-sm" onClick={() => handleApprove(r.id)}>✔</button>
                        <button className="btn btn-red btn-sm"   onClick={() => setRejectModal(r.id)}>✖</button>
                      </div>
                    ) : (
                      r.status === 'LOCKED' ? <span style={{ color:'var(--green-lt)', fontSize:'1rem' }}>🔒</span> : null
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {rejectModal && (
        <RejectModal
          rowId={rejectModal}
          onConfirm={handleReject}
          onCancel={() => setRejectModal(null)}
        />
      )}
    </main>
  );
}
