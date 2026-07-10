const BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `Request failed: ${res.status}`)
  }
  return res.json()
}

export const api = {
  listAccounts: () => request('/accounts'),
  createAccount: (data) => request('/accounts', { method: 'POST', body: JSON.stringify(data) }),
  deleteAccount: (id) => request(`/accounts/${id}`, { method: 'DELETE' }),
  toggleActive: (id) => request(`/accounts/${id}/toggle`, { method: 'PATCH' }),
  setMultiplier: (id, multiplier_override) =>
    request(`/accounts/${id}/multiplier`, { method: 'PATCH', body: JSON.stringify({ multiplier_override }) }),
  autoLogin: (id) => request(`/accounts/${id}/auto-login`, { method: 'POST' }),
  getLoginUrl: (id) => request(`/accounts/${id}/login-url`),
  manualToken: (id, request_token) =>
    request(`/accounts/${id}/manual-token`, { method: 'POST', body: JSON.stringify({ request_token }) }),
  refreshCapital: (id) => request(`/accounts/${id}/refresh-capital`, { method: 'POST' }),
  getLogs: () => request('/logs'),
  getStatus: () => request('/status'),
  getPnl: () => request('/pnl'),
  wsUrl: () => BASE.replace('http', 'ws') + '/ws/live',
}
