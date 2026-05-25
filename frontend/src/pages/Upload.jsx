// src/pages/Upload.jsx
import { useRef, useState } from 'react';
import { api } from '../api';
import { toast } from '../components/Toast';

const ZONES = [
  { key: 'SAP',     label: 'SAP Export (CSV)',       icon: '🏭', sub: 'MB51 flat-file format' },
  { key: 'UTILITY', label: 'Utility Bill Export (CSV)', icon: '⚡', sub: 'Meter readings & kWh' },
  { key: 'TRAVEL',  label: 'Travel Bookings (CSV)',   icon: '✈️', sub: 'Flights, hotels & ground' },
];

function DropZone({ zone, file, onFile, onUpload, loading }) {
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef();

  const handleDrop = (e) => {
    e.preventDefault(); setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) onFile(f);
  };

  return (
    <div
      className={`drop-zone ${dragging ? 'dragging' : ''} ${file ? 'has-file' : ''}`}
      onDragOver={e => { e.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      onClick={() => inputRef.current.click()}
    >
      <input
        ref={inputRef} type="file" accept=".csv"
        onClick={e => e.stopPropagation()}
        onChange={e => { if (e.target.files[0]) onFile(e.target.files[0]); }}
      />
      <div className="drop-zone-icon">{zone.icon}</div>
      <div className="drop-zone-label">{zone.label}</div>
      <div className="drop-zone-sub">{file ? '' : zone.sub}</div>
      {file && <div className="drop-zone-filename">📄 {file.name}</div>}

      <div className="upload-actions" onClick={e => e.stopPropagation()}>
        {file && (
          <button className="btn btn-green btn-full" disabled={loading} onClick={onUpload}>
            {loading ? <><span className="spinner" /> Uploading…</> : `Upload ${zone.key}`}
          </button>
        )}
        {file && (
          <button className="btn btn-ghost btn-full btn-sm" onClick={() => onFile(null)}>
            Clear
          </button>
        )}
      </div>
    </div>
  );
}

export default function Upload() {
  const [files, setFiles] = useState({ SAP: null, UTILITY: null, TRAVEL: null });
  const [loading, setLoading] = useState({ SAP: false, UTILITY: false, TRAVEL: false });

  const setFile = (key, f) => setFiles(p => ({ ...p, [key]: f }));

  const upload = async (key) => {
    if (!files[key]) return;
    setLoading(p => ({ ...p, [key]: true }));
    try {
      const res = await api.upload(files[key], key);
      toast(
        `✅ ${res.rows_created} rows ingested${res.rows_flagged ? `, ${res.rows_flagged} flagged for review` : ''}.`,
        'success'
      );
      setFile(key, null);
    } catch (err) {
      toast(`Upload failed: ${err.message}`, 'error');
    } finally {
      setLoading(p => ({ ...p, [key]: false }));
    }
  };

  return (
    <main className="page">
      <h1 className="page-title">Upload Data</h1>
      <p style={{ color: 'var(--text-secondary)', fontSize: '.9rem', marginBottom: 28 }}>
        Drag and drop a CSV file into the appropriate zone, or click to browse.
        Raw data is stored immutably — uploads cannot overwrite existing records.
      </p>
      <div className="upload-grid">
        {ZONES.map(z => (
          <DropZone
            key={z.key} zone={z}
            file={files[z.key]}
            onFile={f => setFile(z.key, f)}
            onUpload={() => upload(z.key)}
            loading={loading[z.key]}
          />
        ))}
      </div>
    </main>
  );
}
