export default function TradeFeed({ logs }) {
  if (!logs.length) {
    return <div className="empty">No trades replicated yet. This feed fills in as soon as the master account executes an order.</div>
  }

  return (
    <table className="feed">
      <thead>
        <tr>
          <th>Time</th>
          <th>Child</th>
          <th>Symbol</th>
          <th>Side</th>
          <th>Master qty</th>
          <th>Copied qty</th>
          <th>Status</th>
          <th>Note</th>
        </tr>
      </thead>
      <tbody>
        {logs.map((log, i) => (
          <tr key={i}>
            <td>{new Date(log.timestamp).toLocaleTimeString('en-IN')}</td>
            <td>{log.child_account}</td>
            <td>{log.tradingsymbol}</td>
            <td><span className={`tag ${log.transaction_type === 'BUY' ? 'buy' : 'sell'}`}>{log.transaction_type}</span></td>
            <td>{log.master_quantity}</td>
            <td>{log.replicated_quantity}</td>
            <td><span className={`tag ${log.status?.toLowerCase()}`}>{log.status}</span></td>
            <td style={{ color: 'var(--text-muted)' }}>{log.message}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
