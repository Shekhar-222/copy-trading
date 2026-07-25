import { useState } from 'react'

const BROKER_LABELS = { zerodha: 'Zerodha (Kite Connect)', kotak_neo: 'Kotak Neo', angel_one: 'Angel One', groww: 'Groww' }

// Credential fields are write-only (the backend never returns decrypted secrets), so every
// secret input here starts blank and means "leave blank to keep the current stored value" -
// only fields the user actually types into are sent, and only those get changed server-side
// (see main.py's update_account).
export default function EditAccountModal({ account, onClose, onSave }) {
  const [form, setForm] = useState({
    label: account.label,
    client_id: account.client_id,
    api_key: '', api_secret: '', password: '', totp_secret: '', mpin: '', mobile_number: '',
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  const isZerodha = account.broker === 'zerodha'
  const isKotak = account.broker === 'kotak_neo'
  const isAngel = account.broker === 'angel_one'
  const isGroww = account.broker === 'groww'
  const bothGrowwSecretsFilled = isGroww && form.totp_secret && form.api_secret

  const submit = async () => {
    if (bothGrowwSecretsFilled) return
    setSaving(true)
    setError(null)
    try {
      await onSave(account.id, {
        label: form.label !== account.label ? form.label : undefined,
        client_id: form.client_id !== account.client_id ? form.client_id : undefined,
        api_key: form.api_key || undefined,
        api_secret: form.api_secret || undefined,
        password: form.password || undefined,
        totp_secret: form.totp_secret || undefined,
        mpin: form.mpin || undefined,
        mobile_number: form.mobile_number || undefined,
      })
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
        <h2>Edit account — {account.label}</h2>
        <p className="hint">
          Broker can't be changed here — remove and re-add the account instead if you need to switch brokers.
          Leave any credential field below blank to keep its current stored value. Changing a credential clears
          the stored login session, so you'll need to Auto-login again afterwards.
        </p>

        <div className="field">
          <label>Broker</label>
          <input value={BROKER_LABELS[account.broker] || account.broker} disabled />
        </div>
        <div className="field">
          <label>Label</label>
          <input value={form.label} onChange={set('label')} />
        </div>
        <div className="field">
          <label>{isGroww ? 'UCC / Client ID (optional)' : 'Client ID'}</label>
          <input value={form.client_id} onChange={set('client_id')} />
        </div>
        <div className="field">
          <label>API key (leave blank to keep current)</label>
          <input value={form.api_key} onChange={set('api_key')} />
        </div>
        {isZerodha && (
          <div className="field">
            <label>API secret (leave blank to keep current)</label>
            <input type="password" value={form.api_secret} onChange={set('api_secret')} />
          </div>
        )}
        {isZerodha && (
          <div className="field">
            <label>Login password (leave blank to keep current)</label>
            <input type="password" value={form.password} onChange={set('password')} />
          </div>
        )}
        {(isZerodha || isKotak || isAngel) && (
          <div className="field">
            <label>TOTP secret (leave blank to keep current)</label>
            <input value={form.totp_secret} onChange={set('totp_secret')} />
          </div>
        )}
        {isGroww && (
          <>
            <div className="field">
              <label>TOTP secret — only if using a "TOTP" type key (leave blank to keep current)</label>
              <input value={form.totp_secret} onChange={set('totp_secret')} />
            </div>
            <div className="field">
              <label>API secret — only if using an "Approval" type key (leave blank to keep current)</label>
              <input value={form.api_secret} onChange={set('api_secret')} />
            </div>
            <p className="hint" style={{ marginTop: -8 }}>
              An "Approval" key needs a one-off manual approval in the Groww app before its first token can be
              generated - if Auto-login fails with "Session approval required", open the Groww app to approve it,
              or switch to a "TOTP" key for fully hands-off login.
            </p>
            {bothGrowwSecretsFilled && (
              <div className="warning-box">Fill in only one of TOTP secret / API secret, not both — clear the one you're not using.</div>
            )}
          </>
        )}
        {(isKotak || isAngel) && (
          <div className="field">
            <label>{isKotak ? 'MPIN' : 'Trading PIN'} (leave blank to keep current)</label>
            <input value={form.mpin} onChange={set('mpin')} />
          </div>
        )}
        {isKotak && (
          <div className="field">
            <label>Mobile number (leave blank to keep current)</label>
            <input value={form.mobile_number} onChange={set('mobile_number')} />
          </div>
        )}

        {error && <div className="warning-box" style={{ color: 'var(--coral)', borderColor: 'rgba(240,85,76,0.3)' }}>{error}</div>}

        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={saving || bothGrowwSecretsFilled} onClick={submit}>
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}
