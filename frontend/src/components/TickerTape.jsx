import { useEffect, useState } from 'react'
import { api } from '../api'

const fmt = (n) => n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export default function TickerTape() {
  const [indices, setIndices] = useState([])

  useEffect(() => {
    let alive = true
    const fetchTicker = () =>
      api.getTicker()
        .then((d) => { if (alive) setIndices(d.indices || []) })
        .catch(() => {}) // a quote hiccup shouldn't disturb the dashboard
    fetchTicker()
    const poll = setInterval(fetchTicker, 5000)
    return () => { alive = false; clearInterval(poll) }
  }, [])

  if (!indices.length) return null

  // Repeat the items so one "half" of the track is wide enough to cover the viewport;
  // the track holds two identical halves and the animation slides exactly one half's
  // width, so the loop point is invisible.
  const half = Array.from({ length: 4 }, () => indices).flat()

  const renderItem = (idx, key) => {
    const up = idx.change >= 0
    return (
      <span className="ticker-item" key={key}>
        <span className="ticker-symbol">{idx.symbol}</span>
        <span className="ticker-price">{fmt(idx.last_price)}</span>
        <span className={up ? 'ticker-up' : 'ticker-down'}>
          {up ? '▲' : '▼'} {fmt(Math.abs(idx.change))} ({up ? '+' : '-'}{fmt(Math.abs(idx.change_pct))}%)
        </span>
      </span>
    )
  }

  return (
    <div className="ticker-tape" title="Live index prices — hover to pause">
      <div className="ticker-track">
        <div className="ticker-half">{half.map((idx, i) => renderItem(idx, `a${i}`))}</div>
        <div className="ticker-half" aria-hidden="true">{half.map((idx, i) => renderItem(idx, `b${i}`))}</div>
      </div>
    </div>
  )
}
