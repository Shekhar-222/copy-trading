import { useState, useEffect } from 'react'
import { api } from '../api'

export default function TokenModal({ account, onClose, onDone }) {
  const [loginUrl, setLoginUrl] = useState(null)
  const [token, setToken] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.getLoginUrl(account.id).then((r) => setLoginUrl(r.login_url))
  }, [account.id])

  const submit = async () => {
    setSaving(true)
    setError(null)
    try {
      await api.manualToken(account.id, token.trim())
      onDone()
      onClose()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Manual login — {account.label}</h2>
        <p className="hint">
          1. Open the link below and log in as usual.<br />
          2. After login you'll be redirected to a URL containing <code>request_token=...</code>.<br />
          3. Copy just that token value and paste it here.
        </p>
        {loginUrl && (
          <a className="btn" style={{ display: 'inline-block', marginBottom: 14, textDecoration: 'none' }} href={loginUrl} target="_blank" rel="noreferrer">
            Open login page ↗
          </a>
        )}
        <div className="field">
          <label>request_token</label>
          <input value={token} onChange={(e) => setToken(e.target.value)} placeholder="paste token here" />
        </div>
        {error && <div className="warning-box" style={{ color: 'var(--coral)', borderColor: 'rgba(240,85,76,0.3)' }}>{error}</div>}
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={!token || saving} onClick={submit}>
            {saving ? 'Verifying…' : 'Save token'}
          </button>
        </div>
      </div>
    </div>
  )
}
