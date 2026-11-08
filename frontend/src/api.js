const BASE = import.meta.env.VITE_API_BASE || '/api'

function csrf() {
  const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/)
  return m ? decodeURIComponent(m[1]) : ''
}

async function request(path, { method = 'GET', body, raw = false } = {}) {
  const headers = {}
  let payload = body
  if (body && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  if (method !== 'GET') headers['X-CSRFToken'] = csrf()

  const res = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: payload,
    credentials: 'include',
  })
  if (raw) return res
  const text = await res.text()
  const data = text ? JSON.parse(text) : null
  if (!res.ok) {
    const detail = data?.detail || Object.values(data || {}).flat().join('; ') || `HTTP ${res.status}`
    throw new Error(detail)
  }
  return data
}

export const api = {
  layerTypes: () => request('/layer-types/'),
  comparatorDefaults: () => request('/comparator-defaults/'),

  corpora: () => request('/corpora/'),
  ingestVocadito: (id, body) =>
    request(`/corpora/${id}/ingest-vocadito/`, { method: 'POST', body }),
  validateCorpus: (id) => request(`/corpora/${id}/validate/`, { method: 'POST', body: {} }),

  items: (params) => request(`/items/?${new URLSearchParams(params)}`),
  item: (id) => request(`/items/${id}/`),
  itemIssues: (id) => request(`/items/${id}/issues/?open=true`),
  validateItem: (id) => request(`/items/${id}/validate/`, { method: 'POST', body: {} }),
  uploadAnnotation: (id, formData) =>
    request(`/items/${id}/upload-annotation/`, { method: 'POST', body: formData }),

  layers: (params) => request(`/layers/?${new URLSearchParams(params)}`),
  segments: (layerId) => request(`/layers/${layerId}/segments/?resolved=true`),
  lineage: (layerId) => request(`/layers/${layerId}/lineage/`),
  validateLayer: (id) => request(`/layers/${id}/validate/`, { method: 'POST', body: {} }),
  review: (id, body) => request(`/layers/${id}/reviews/`, { method: 'POST', body }),
  reviews: (id) => request(`/layers/${id}/reviews/`),

  addCorrection: (body) => request('/corrections/', { method: 'POST', body }),
  corrections: (layerId) => request(`/corrections/?layer=${layerId}`),

  compare: (body) => request('/comparisons/', { method: 'POST', body }),
  acknowledge: (issueId, note) =>
    request(`/validation-issues/${issueId}/acknowledge/`, { method: 'POST', body: { note } }),

  svlUrl: (layerId) => `${BASE}/layers/${layerId}/export-svl/`,
  sessionUrl: (itemId) => `${BASE}/items/${itemId}/export-session/`,
}
