const BASE = import.meta.env.VITE_API_BASE || '/api'

function csrf() {
  const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/)
  return m ? decodeURIComponent(m[1]) : ''
}

/*
  What a reader is told when a request fails.

  A 400 carries this project's own text: it names the field, the value and what was
  expected, which is the most useful sentence anyone could write about that failure, so it
  is passed through. Every other status arrives in the framework's words —
  "Authentication credentials were not provided" describes an HTTP exchange the reader
  never saw and cannot act on — so the app answers for itself.
*/
function failureMessage(status, data) {
  if (status === 400) {
    const own = data?.detail || Object.values(data || {}).flat().join('; ')
    if (own) return own
  }
  if (status === 403 && String(data?.detail || '').startsWith('CSRF')) {
    // Signed in, but the browser's token no longer matches the session.
    return 'This sign-in has expired. Sign in again to make this change.'
  }
  if (status === 401 || status === 403) return 'Sign in to make this change.'
  if (status === 404) return 'That is no longer here. This page may have been open a while.'
  if (status === 413) return 'That file is too large for the server to accept.'
  if (status >= 500) return 'The server could not finish that request.'
  return 'That request could not be completed.'
}

async function request(path, { method = 'GET', body, raw = false } = {}) {
  const headers = {}
  let payload = body
  if (body && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  if (method !== 'GET') headers['X-CSRFToken'] = csrf()

  let res
  try {
    res = await fetch(`${BASE}${path}`, {
      method,
      headers,
      body: payload,
      credentials: 'include',
    })
  } catch {
    // fetch rejects only when the request never got an answer.
    throw new Error('The app could not reach the server. Check that it is running, then try again.')
  }
  if (raw) return res
  const text = await res.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    // A failure can arrive as a server error page. Its status still says what happened.
    data = null
  }
  if (!res.ok) throw new Error(failureMessage(res.status, data))
  return data
}

export const api = {
  me: () => request('/me/'),
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

  comparisons: (params) => request(`/comparisons/?${new URLSearchParams(params)}`),
  compare: (body) => request('/comparisons/', { method: 'POST', body }),
  acknowledge: (issueId, note) =>
    request(`/validation-issues/${issueId}/acknowledge/`, { method: 'POST', body: { note } }),

  svlUrl: (layerId) => `${BASE}/layers/${layerId}/export-svl/`,
  sessionUrl: (itemId) => `${BASE}/items/${itemId}/export-session/`,
}
