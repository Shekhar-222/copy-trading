import { useEffect, useState } from 'react'
import { api } from '../api'

export default function PositionsModal({ account, onClose }) {
  const [loading, setLoading] = useState(true)
  const [positions, setPositions] = useState([])
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api.getPositions(account.id)
      .then((data) => { if (!cancelled) setPositions(data) })
      .catch((e) => { if (!cancelled) setError(e.message) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [account.id])

  const sorted = [...positions].sort((a, b) => (a.status === 'OPEN' ? 0 : 1) - (b.status === 'OPEN' ? 0 : 1))
  const totalPnl = sorted.reduce((sum, p) => sum + (p.pnl || 0), 0)

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal positions-modal" onClick={(e) => e.stopPropagation()}>
        <h2>Positions — {account.label}</h2>
        <p className="hint">Open and closed (squared-off today) positions, fetched live.</p>

        {loading ? (
          <div className="positions-empty">Loading…</div>
        ) : error ? (
          <div className="warning-box" style={{ color: 'var(--coral)', borderColor: 'rgba(240,85,76,0.3)' }}>{error}</div>
        ) : !sorted.length ? (
          <div className="positions-empty">No positions today.</div>
        ) : (
          <table className="positions-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Status</th>
                <th>Qty</th>
                <th>Avg</th>
                <th>P&L</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((p, i) => (
                <tr key={i}>
                  <td>{p.tradingsymbol}</td>
                  <td>
                    <span className={`tag ${p.status === 'OPEN' ? 'open' : 'closed'}`}>{p.status}</span>
                  </td>
                  <td className={p.quantity > 0 ? 'buy' : p.quantity < 0 ? 'sell' : ''}>{p.quantity}</td>
                  <td>₹{p.average_price?.toLocaleString('en-IN')}</td>
                  <td className={p.pnl >= 0 ? 'up' : 'down'}>
                    {p.pnl >= 0 ? '+' : ''}₹{p.pnl?.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="positions-total-row">
                <td colSpan={4}>Total P&L</td>
                <td className={totalPnl >= 0 ? 'up' : 'down'}>
                  {totalPnl >= 0 ? '+' : ''}₹{totalPnl.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                </td>
              </tr>
            </tfoot>
          </table>
        )}

        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  )
}
