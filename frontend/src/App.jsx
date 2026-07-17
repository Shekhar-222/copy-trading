import { useEffect, useState, useRef, useCallback } from 'react'
import { api, accessToken } from './api'
import MasterCard from './components/MasterCard'
import ChildCard from './components/ChildCard'
import PnlBadge from './components/PnlBadge'
import TradeFeed from './components/TradeFeed'
import AddAccountModal from './components/AddAccountModal'
import TokenModal from './components/TokenModal'
import ConfirmModal from './components/ConfirmModal'
import PositionsModal from './components/PositionsModal'
import TickerTape from './components/TickerTape'

export default function App() {
  const [accounts, setAccounts] = useState([])
  const [logs, setLogs] = useState([])
  const [status, setStatus] = useState({ masters: [] })
  const [pnl, setPnl] = useState({ accounts: [], total: 0 })
  const [showAdd, setShowAdd] = useState(false)
  const [tokenModalAccount, setTokenModalAccount] = useState(null)
  const [deleteTarget, setDeleteTarget] = useState(null)
  const [exitTarget, setExitTarget] = useState(null)
  const [exiting, setExiting] = useState(false)
  const [positionsTarget, setPositionsTarget] = useState(null)
  const [now, setNow] = useState(new Date())
  const wsRef = useRef(null)

  const load = useCallback(async () => {
    const [accs, l, s, p] = await Promise.all([api.listAccounts(), api.getLogs(), api.getStatus(), api.getPnl()])
    setAccounts(accs)
    setLogs(l)
    setStatus(s)
    setPnl(p)
  }, [])

  useEffect(() => {
    load()
    const ws = new WebSocket(api.wsUrl())
    ws.onopen = () => {
      const token = accessToken.get()
      if (token) ws.send(token) // first message doubles as the WS auth handshake - see main.py
    }
    ws.onmessage = (evt) => {
      const payload = JSON.parse(evt.data)
      if (payload.event === 'pnl') {
        // tick-driven P&L push - swap state directly, no REST round-trip
        setPnl(payload)
      } else if (payload.status) {
        setLogs((prev) => [payload, ...prev].slice(0, 200))
        api.getPnl().then(setPnl)
      } else {
        // status/connection events - just re-sync
        load()
      }
    }
    wsRef.current = ws
    const poll = setInterval(load, 15000)
    return () => { ws.close(); clearInterval(poll) }
  }, [load])

  useEffect(() => {
    const clock = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(clock)
  }, [])

  const masters = accounts.filter((a) => a.role === 'master')
  const children = accounts.filter((a) => a.role === 'child')
  const masterCapital = masters[0]?.capital || 0

  const listeningFor = (id) => status.masters.find((m) => m.id === id)?.listening
  const pnlFor = (id) => pnl.accounts.find((p) => p.id === id)?.pnl ?? null

  const handleCreate = async (data) => {
    await api.createAccount(data)
    await load()
  }
  const handleDelete = async (id) => { await api.deleteAccount(id); await load() }
  const handleToggle = async (id) => { await api.toggleActive(id); await load() }
  const handleSwitchRole = async (id, role) => {
    try { await api.switchRole(id, role); await load() }
    catch (e) { alert(e.message) }
  }
  const handleAutoLogin = async (id) => {
    try { await api.autoLogin(id); await load() }
    catch (e) { alert(e.message) }
  }
  const handleSetMultiplier = async (id, val) => { await api.setMultiplier(id, val); await load() }
  const handleExit = async (id) => {
    setExiting(true)
    try {
      await api.exitPositions(id)
      await load()
    } catch (e) {
      alert(e.message)
    } finally {
      setExiting(false)
    }
  }

  return (
    <div className="app">
      <div className="topbar">
        <h1><span className="dot" /> Copy Trading </h1>
        <div className="row" style={{ gap: 16 }}>
          <span className="sub">
            {now.toLocaleDateString('en-IN', { weekday: 'short', day: '2-digit', month: 'short' })}
            {' · '}
            <span className="clock">{now.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true })}</span>
            {' · NSE F&O'}
          </span>
          <PnlBadge label="Aggregate P&L" value={pnl.total} layout="pill" size="lg" />
        </div>
      </div>

      <TickerTape />

      <div className="main">
        <div className="section-title">Master account</div>
        {masters.length === 0 && <div className="empty">No master account yet. Add one to start copying its trades.</div>}
        {masters.map((m) => (
          <MasterCard
            key={m.id}
            account={m}
            listening={listeningFor(m.id)}
            pnl={pnlFor(m.id)}
            onToggle={handleToggle}
            onLogin={handleAutoLogin}
            onManualLogin={setTokenModalAccount}
            onDelete={setDeleteTarget}
            onExit={setExitTarget}
            onShowPositions={setPositionsTarget}
            onSwitchRole={handleSwitchRole}
          />
        ))}

        <div className="section-title" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span>Child accounts ({children.length})</span>
          <button className="btn primary small" onClick={() => setShowAdd(true)}>+ Add account</button>
        </div>
        {children.length === 0 ? (
          <div className="empty">No child accounts yet.</div>
        ) : (
          <div className="children-grid">
            {children.map((c) => (
              <ChildCard
                key={c.id}
                account={c}
                masterCapital={masterCapital}
                pnl={pnlFor(c.id)}
                onToggle={handleToggle}
                onLogin={handleAutoLogin}
                onManualLogin={setTokenModalAccount}
                onDelete={setDeleteTarget}
                onSetMultiplier={handleSetMultiplier}
                onExit={setExitTarget}
                onShowPositions={setPositionsTarget}
                onSwitchRole={handleSwitchRole}
              />
            ))}
          </div>
        )}

        <div className="section-title">Live replication feed</div>
        <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
          <div className="table-scroll">
            <TradeFeed logs={logs} />
          </div>
        </div>
      </div>

      {showAdd && <AddAccountModal onClose={() => setShowAdd(false)} onCreate={handleCreate} />}
      {tokenModalAccount && (
        <TokenModal account={tokenModalAccount} onClose={() => setTokenModalAccount(null)} onDone={load} />
      )}
      {deleteTarget && (
        <ConfirmModal
          title={`Remove ${deleteTarget.role} account?`}
          message={`This will permanently remove "${deleteTarget.label}" (${deleteTarget.client_id}) from copy trading.`}
          confirmLabel="Remove"
          onConfirm={() => handleDelete(deleteTarget.id)}
          onClose={() => setDeleteTarget(null)}
        />
      )}
      {exitTarget && (
        <ConfirmModal
          title={`Exit all positions on ${exitTarget.label}?`}
          message={`This immediately cancels every pending order and squares off every open position on "${exitTarget.label}" (${exitTarget.client_id}) at the current market price. This cannot be undone.`}
          confirmLabel={exiting ? 'Exiting…' : 'Exit all'}
          onConfirm={() => handleExit(exitTarget.id)}
          onClose={() => setExitTarget(null)}
        />
      )}
      {positionsTarget && (
        <PositionsModal account={positionsTarget} onClose={() => setPositionsTarget(null)} />
      )}
    </div>
  )
}
