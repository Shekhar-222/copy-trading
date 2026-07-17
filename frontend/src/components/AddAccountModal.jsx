import { useState } from 'react'

export default function AddAccountModal({ onClose, onCreate }) {
  const [form, setForm] = useState({
    label: '', role: 'child', broker: 'zerodha', client_id: '', api_key: '', api_secret: '',
    password: '', totp_secret: '', mpin: '', mobile_number: '',
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  const setBroker = (e) => {
    const broker = e.target.value
    // Kotak Neo and Angel One are child-only for now - force role and drop irrelevant fields.
    setForm({
      ...form,
      broker,
      role: broker === 'kotak_neo' || broker === 'angel_one' ? 'child' : form.role,
      api_secret: '', password: '', mpin: '', mobile_number: '',
    })
  }

  const submit = async () => {
    setSaving(true)
    setError(null)
    try {
      await onCreate({
        ...form,
        password: form.password || undefined,
        api_secret: form.api_secret || undefined,
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

  const isKotak = form.broker === 'kotak_neo'
  const isAngel = form.broker === 'angel_one'
  const canSave = form.label && form.client_id && (
    isKotak ? form.api_key && form.totp_secret && form.mpin && form.mobile_number
    : isAngel ? form.api_key && form.totp_secret && form.mpin
    : form.api_key && form.api_secret && form.totp_secret
  )

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Add account</h2>

        <div className="field">
          <label>Broker</label>
          <select value={form.broker} onChange={setBroker}>
            <option value="zerodha">Zerodha (Kite Connect)</option>
            <option value="kotak_neo">Kotak Neo</option>
            <option value="angel_one">Angel One</option>
          </select>
        </div>

        {isAngel ? (
          <>
            <p className="hint">
              Angel One accounts can only be added as child accounts for now (the master feed stays on Zerodha).
              Create a SmartAPI app at smartapi.angelbroking.com for the API key, and complete TOTP registration
              on the Angel One app too.
            </p>
            <div className="warning-box">
              Trading PIN and TOTP secret are encrypted at rest, but only run this on a machine you trust — Angel
              One login has no manual/browser fallback, it always uses these credentials directly.
            </div>

            <div className="field">
              <label>Label</label>
              <input placeholder="e.g. Angel One child" value={form.label} onChange={set('label')} />
            </div>
            <div className="field">
              <label>Role</label>
              <input value="Child (receives copied trades)" disabled />
            </div>
            <div className="field">
              <label>Client code</label>
              <input placeholder="e.g. A123456" value={form.client_id} onChange={set('client_id')} />
            </div>
            <div className="field">
              <label>SmartAPI key</label>
              <input value={form.api_key} onChange={set('api_key')} />
            </div>
            <div className="field">
              <label>TOTP secret (base32, from Angel One TOTP registration)</label>
              <input value={form.totp_secret} onChange={set('totp_secret')} />
            </div>
            <div className="field">
              <label>Trading PIN</label>
              <input type="password" value={form.mpin} onChange={set('mpin')} />
            </div>
          </>
        ) : isKotak ? (
          <>
            <p className="hint">
              Kotak Neo accounts can only be added as child accounts for now (the master feed stays on Zerodha).
              Generate a Trade API application on the Kotak Neo app/web (Invest tab → Trade API card) for the
              consumer key, and complete TOTP registration there too.
            </p>
            <div className="warning-box">
              MPIN and TOTP secret are encrypted at rest, but only run this on a machine you trust — Kotak Neo
              login has no manual/browser fallback, it always uses these credentials directly.
            </div>

            <div className="field">
              <label>Label</label>
              <input placeholder="e.g. Kotak Neo child" value={form.label} onChange={set('label')} />
            </div>
            <div className="field">
              <label>Role</label>
              <input value="Child (receives copied trades)" disabled />
            </div>
            <div className="field">
              <label>UCC (unique client code)</label>
              <input placeholder="e.g. ABC12" value={form.client_id} onChange={set('client_id')} />
            </div>
            <div className="field">
              <label>Consumer key</label>
              <input value={form.api_key} onChange={set('api_key')} />
            </div>
            <div className="field">
              <label>Registered mobile number</label>
              <input placeholder="e.g. +919999996708" value={form.mobile_number} onChange={set('mobile_number')} />
            </div>
            <div className="field">
              <label>TOTP secret (base32, from Kotak Neo TOTP registration)</label>
              <input value={form.totp_secret} onChange={set('totp_secret')} />
            </div>
            <div className="field">
              <label>MPIN</label>
              <input type="password" value={form.mpin} onChange={set('mpin')} />
            </div>
          </>
        ) : (
          <>
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
          </>
        )}

        {error && <div className="warning-box" style={{ color: 'var(--coral)', borderColor: 'rgba(240,85,76,0.3)' }}>{error}</div>}

        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={saving || !canSave} onClick={submit}>
            {saving ? 'Saving…' : 'Add account'}
          </button>
        </div>
      </div>
    </div>
  )
}
