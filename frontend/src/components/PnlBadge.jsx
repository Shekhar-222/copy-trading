export default function PnlBadge({ label, value, size = 'md', layout = 'row' }) {
  const known = typeof value === 'number' && !Number.isNaN(value)
  const tone = !known ? 'flat' : value > 0 ? 'up' : value < 0 ? 'down' : 'flat'
  const amount = known ? Math.abs(value).toLocaleString('en-IN', { maximumFractionDigits: 0 }) : null
  const text = known ? `${value < 0 ? '-' : value > 0 ? '+' : ''}₹${amount}` : '—'

  return (
    <div className={`pnl-badge ${layout} ${tone} ${size}`}>
      {label && <span className="pnl-badge-label">{label}</span>}
      <span className="pnl-badge-value">
        {tone !== 'flat' && <span className="pnl-arrow">{tone === 'up' ? '▲' : '▼'}</span>}
        {text}
      </span>
    </div>
  )
}
