// src/api.js — central API client
const BASE = import.meta.env.VITE_API_BASE_URL || '';
const CLIENT_ID = import.meta.env.VITE_CLIENT_ID || '1';

async function request(path, opts = {}) {
  const url = `${BASE}${path}`;
  const res = await fetch(url, { credentials: 'include', ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
  return data;
}

export const api = {
  clientId: () => CLIENT_ID,

  upload(file, sourceType) {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('source_type', sourceType);
    fd.append('client_id', CLIENT_ID);
    return request('/api/upload/', { method: 'POST', body: fd });
  },

  rows(params = {}) {
    const q = new URLSearchParams({ client_id: CLIENT_ID, ...params });
    return request(`/api/rows/?${q}`);
  },

  approve(id) {
    return request(`/api/rows/${id}/approve/`, { method: 'PATCH',
      headers: { 'Content-Type': 'application/json' } });
  },

  reject(id, reason) {
    return request(`/api/rows/${id}/reject/`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
    });
  },

  bulkApprove(rowIds) {
    return request('/api/rows/bulk-approve/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ row_ids: rowIds }),
    });
  },

  summary() {
    return request(`/api/summary/?client_id=${CLIENT_ID}`);
  },

  auditLog(params = {}) {
    const q = new URLSearchParams({ client_id: CLIENT_ID, ...params });
    return request(`/api/audit-log/?${q}`);
  },
};
