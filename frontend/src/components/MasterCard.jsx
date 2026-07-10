import PnlBadge from './PnlBadge'

export default function MasterCard({ account, listening, pnl, onToggle, onLogin, onManualLogin, onDelete }) {
  const pillClass = listening ? 'live' : account.has_token_today ? 'off' : 'stale'
  const pillText = listening ? 'LISTENING' : account.has_token_today ? 'READY' : 'TOKEN EXPIRED'

  return (
    <div className="card master-card">
      <div>
        <div className="row">
          <b>{account.label}</b>
          <span className={`status-pill ${pillClass}`}>{pillText}</span>
        </div>
        <div className="id">{account.client_id} · master</div>
        {account.real_name && (
          <div className="real-name" title="Fetched from the Zerodha profile">
            <span className="verified-glyph">✓</span> {account.real_name}
          </div>
        )}
      </div>
      <PnlBadge label="Running P&L" value={pnl} layout="pill" />
      <div className="row">
        {!account.has_token_today && (
          <>
            <button className="btn small" onClick={() => onLogin(account.id)}>Auto-login</button>
            <button className="btn small" onClick={() => onManualLogin(account)}>Manual token</button>
          </>
        )}
        <button className="btn small" disabled={!account.has_token_today} onClick={() => onToggle(account.id)}>
          {account.active ? 'Pause copying' : 'Resume copying'}
        </button>
        <button className="btn small danger" onClick={() => onDelete(account)}>Remove</button>
      </div>
    </div>
  )
}
