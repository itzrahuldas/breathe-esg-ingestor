// src/components/RejectModal.jsx
import { useState } from 'react';

export default function RejectModal({ rowId, onConfirm, onCancel }) {
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(false);

  const submit = async () => {
    if (!reason.trim()) return;
    setLoading(true);
    await onConfirm(rowId, reason.trim());
    setLoading(false);
  };

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <h3 className="modal-title">Reject this row</h3>
        <p style={{ fontSize: '.85rem', color: 'var(--text-secondary)', marginBottom: 14 }}>
          Provide a reason. This will be recorded in the audit log.
        </p>
        <textarea
          className="textarea"
          placeholder="e.g. Quantity appears incorrect — please resubmit with verified meter reading."
          value={reason}
          onChange={e => setReason(e.target.value)}
          autoFocus
        />
        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onCancel}>Cancel</button>
          <button
            className="btn btn-red"
            onClick={submit}
            disabled={!reason.trim() || loading}
          >
            {loading ? <span className="spinner" /> : 'Reject Row'}
          </button>
        </div>
      </div>
    </div>
  );
}
