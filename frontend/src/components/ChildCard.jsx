import { useState } from 'react'
import PnlBadge from './PnlBadge'

export default function ChildCard({ account, masterCapital, pnl, onToggle, onLogin, onManualLogin, onDelete, onSetMultiplier }) {
  const ratio = masterCapital > 0 ? Math.min((account.capital / masterCapital) * 100, 100) : 0
  const effectiveMultiplier = account.multiplier_override ?? (masterCapital > 0 ? account.capital / masterCapital : 0)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')

  const startEdit = () => {
    setDraft(account.multiplier_override != null ? String(account.multiplier_override) : '')
    setEditing(true)
  }

  const commit = () => {
    const trimmed = draft.trim()
    if (trimmed === '') {
      onSetMultiplier(account.id, null)
    } else {
      const num = parseFloat(trimmed)
      if (Number.isNaN(num) || num < 0) { setEditing(false); return }
      onSetMultiplier(account.id, num)
    }
    setEditing(false)
  }

  return (
    <div className="card child-card">
      <div className="head">
        <span className="label">{account.label}</span>
        <span className={`status-pill ${account.has_token_today ? 'live' : 'stale'}`}>
          {account.has_token_today ? 'READY' : 'NO TOKEN'}
        </span>
      </div>
      <div className="id" style={{ marginTop: -6 }}>{account.client_id} · child</div>
      {account.real_name && (
        <div className="real-name" title="Fetched from the Zerodha profile">
          <span className="verified-glyph">✓</span> {account.real_name}
        </div>
      )}

      <div className="ratio-bar-track">
        <div className="ratio-bar-fill" style={{ width: `${ratio}%` }} />
      </div>
      <div className="capital-row">
        <span>Capital</span>
        <b>₹{account.capital?.toLocaleString('en-IN') ?? '—'}</b>
      </div>
      <PnlBadge label="Running P&L" value={pnl} />
      <div className="multiplier-row">
        <span>{account.multiplier_override != null ? 'Manual multiplier' : 'Capital ratio'}</span>
        {editing ? (
          <div className="multiplier-edit">
            <input
              autoFocus
              type="number"
              step="0.01"
              min="0"
              className="multiplier-input"
              placeholder="auto"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commit()
                if (e.key === 'Escape') setEditing(false)
              }}
              onBlur={commit}
            />
            <button className="icon-btn confirm" title="Save" onMouseDown={(e) => e.preventDefault()} onClick={commit}>✓</button>
            <button className="icon-btn cancel" title="Cancel" onMouseDown={(e) => e.preventDefault()} onClick={() => setEditing(false)}>✕</button>
          </div>
        ) : (
          <button className="multiplier-value" onClick={startEdit} title="Click to set a manual multiplier">
            {effectiveMultiplier.toFixed(3)}×
            <span className="edit-glyph">✎</span>
          </button>
        )}
      </div>

      <div className="row" style={{ marginTop: 4, flexWrap: 'wrap' }}>
        {!account.has_token_today && (
          <>
            <button className="btn small" onClick={() => onLogin(account.id)}>Auto-login</button>
            <button className="btn small" onClick={() => onManualLogin(account)}>Manual token</button>
          </>
        )}
        <button
          className={`btn small ${account.active ? 'danger' : 'success'}`}
          onClick={() => onToggle(account.id)}
        >
          {account.active ? 'Exclude' : 'Include'}
        </button>
        <button className="btn small danger" onClick={() => onDelete(account)}>Remove</button>
      </div>
    </div>
  )
}
