const BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'
const TOKEN_KEY = 'copytrader_access_token'

export const accessToken = {
  get: () => localStorage.getItem(TOKEN_KEY) || '',
  set: (token) => localStorage.setItem(TOKEN_KEY, token),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}

// Used by AccessGate to check a candidate token before storing it - a plain fetch (not
// `request()` above) since a wrong/missing token should just report false, not clear
// storage or force a reload.
export async function verifyAccessToken(token) {
  try {
    const res = await fetch(`${BASE}/status`, { headers: { 'X-Access-Token': token } })
    return res.ok
  } catch {
    return false
  }
}

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', 'X-Access-Token': accessToken.get() },
    ...options,
  })
  if (res.status === 401) {
    accessToken.clear()
    window.location.reload() // drop back to the unlock screen
    throw new Error('Unauthorized')
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `Request failed: ${res.status}`)
  }
  return res.json()
}

export const api = {
  listAccounts: () => request('/accounts'),
  createAccount: (data) => request('/accounts', { method: 'POST', body: JSON.stringify(data) }),
  updateAccount: (id, data) => request(`/accounts/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteAccount: (id) => request(`/accounts/${id}`, { method: 'DELETE' }),
  toggleActive: (id) => request(`/accounts/${id}/toggle`, { method: 'PATCH' }),
  switchRole: (id, role) => request(`/accounts/${id}/role`, { method: 'PATCH', body: JSON.stringify({ role }) }),
  setMultiplier: (id, multiplier_override) =>
    request(`/accounts/${id}/multiplier`, { method: 'PATCH', body: JSON.stringify({ multiplier_override }) }),
  autoLogin: (id) => request(`/accounts/${id}/auto-login`, { method: 'POST' }),
  getLoginUrl: (id) => request(`/accounts/${id}/login-url`),
  manualToken: (id, request_token) =>
    request(`/accounts/${id}/manual-token`, { method: 'POST', body: JSON.stringify({ request_token }) }),
  refreshCapital: (id) => request(`/accounts/${id}/refresh-capital`, { method: 'POST' }),
  exitPositions: (id) => request(`/accounts/${id}/exit`, { method: 'POST' }),
  getPositions: (id) => request(`/accounts/${id}/positions`),
  getLogs: () => request('/logs'),
  getTicker: () => request('/ticker'),
  getStatus: () => request('/status'),
  getPnl: () => request('/pnl'),
  // Token is sent as the WebSocket's first message (see App.jsx), not a query param here -
  // a query param would end up in uvicorn's plaintext access log on every connection.
  wsUrl: () => BASE.replace('http', 'ws') + '/ws/live',
}
