import { useState } from 'react'

export default function AddAccountModal({ onClose, onCreate }) {
  const [form, setForm] = useState({
    label: '', role: 'child', client_id: '', api_key: '', api_secret: '',
    password: '', totp_secret: '',
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  const submit = async () => {
    setSaving(true)
    setError(null)
    try {
      await onCreate({ ...form, password: form.password || undefined })
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
        <h2>Add Zerodha account</h2>
        <p className="hint">Create a Kite Connect app for this account at developers.kite.trade first — you'll need its API key and secret.</p>

        <div className="warning-box">
          Password is optional and only used for fully automated daily login. Without it, you'll paste a login token manually each morning instead. Everything is encrypted at rest, but only run this on a machine you trust.
        </div>

        <div className="field">
          <label>Label</label>
          <input placeholder="e.g. Main account" value={form.label} onChange={set('label')} />
        </div>
        <div className="field">
          <label>Role</label>
          <select value={form.role} onChange={set('role')}>
            <option value="master">Master (source of trades)</option>
            <option value="child">Child (receives copied trades)</option>
          </select>
        </div>
        <div className="field">
          <label>Zerodha client ID</label>
          <input placeholder="e.g. AJ230321" value={form.client_id} onChange={set('client_id')} />
        </div>
        <div className="field">
          <label>Kite Connect API key</label>
          <input value={form.api_key} onChange={set('api_key')} />
        </div>
        <div className="field">
          <label>Kite Connect API secret</label>
          <input type="password" value={form.api_secret} onChange={set('api_secret')} />
        </div>
        <div className="field">
          <label>TOTP secret (base32, from Kite 2FA setup)</label>
          <input value={form.totp_secret} onChange={set('totp_secret')} />
        </div>
        <div className="field">
          <label>Login password (optional — enables auto-login)</label>
          <input type="password" value={form.password} onChange={set('password')} />
        </div>

        {error && <div className="warning-box" style={{ color: 'var(--coral)', borderColor: 'rgba(240,85,76,0.3)' }}>{error}</div>}

        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={saving || !form.label || !form.client_id} onClick={submit}>
            {saving ? 'Saving…' : 'Add account'}
          </button>
        </div>
      </div>
    </div>
  )
}
