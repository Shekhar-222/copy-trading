import PnlBadge from './PnlBadge'

export default function MasterCard({ account, listening, pnl, onToggle, onLogin, onManualLogin, onEdit, onDelete, onExit, onShowPositions, onSwitchRole }) {
  const pillClass = listening ? 'live' : account.has_token_today ? 'off' : 'stale'
  const pillText = listening ? 'LISTENING' : account.has_token_today ? 'READY' : 'TOKEN EXPIRED'

  return (
    <div className={`card master-card ${listening ? 'listening' : 'not-listening'}`}>
      <div className="master-header">
        <div className="master-header-left">
          <b className="master-name">{account.label}</b>
        </div>
        <div className="master-header-center">
          <span className="master-badge">Master</span>
        </div>
        <div className="master-header-right" />
      </div>

      <span className={`status-pill ${pillClass} master-status-pill`}>
        {listening && <span className="pill-dot" />}
        {pillText}
      </span>

      <div className="master-body">
        <div className="master-info-row">
          <div>
            <div className="id">{account.client_id} · Zerodha</div>
            {account.real_name && (
              <div className="real-name" title="Fetched from the Zerodha profile">
                <span className="verified-glyph">✓</span> {account.real_name}
              </div>
            )}
            <div className="capital-row master-capital">
              <span>Capital : </span>
              <b>₹{account.capital?.toLocaleString('en-IN') ?? '—'}</b>
            </div>
          </div>
          <PnlBadge label="Running P&L" value={pnl} layout="pill" />
        </div>

        <button
          className={`toggle-pill ${account.active ? 'active' : 'inactive'}`}
          disabled={!account.has_token_today}
          onClick={() => onToggle(account.id)}
        >
          {account.active ? 'Stop ' : 'Start Trading'}
        </button>

        <div className="row master-actions">
          {!account.has_token_today && (
            <>
              <button className="btn small" onClick={() => onLogin(account.id)}>Auto-login</button>
              <button className="btn small" onClick={() => onManualLogin(account)}>Manual token</button>
            </>
          )}
          <button className="btn small" disabled={!account.has_token_today} onClick={() => onShowPositions(account)}>
            Positions
          </button>
          <button className="btn small danger" disabled={!account.has_token_today} onClick={() => onExit(account)}>
            Exit all
          </button>
          <button className="btn small" onClick={() => onEdit(account)}>Edit</button>
          <button
            className="btn small"
            disabled={account.active}
            title={account.active ? 'Stop trading first to switch this account to a child' : 'Switch to child'}
            onClick={() => onSwitchRole(account.id, 'child')}
          >
            Switch to child
          </button>
          <button className="btn small danger" onClick={() => onDelete(account)}>Remove</button>
        </div>
      </div>
    </div>
  )
}
