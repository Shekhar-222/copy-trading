import { useEffect, useState } from 'react'
import { accessToken, verifyAccessToken } from '../api'

export default function AccessGate({ children }) {
  const [checking, setChecking] = useState(true)
  const [unlocked, setUnlocked] = useState(false)
  const [input, setInput] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    const stored = accessToken.get()
    verifyAccessToken(stored).then((ok) => {
      setUnlocked(ok)
      setChecking(false)
    })
  }, [])

  const handleSubmit = async (e) => {
    e.preventDefault()
    setSubmitting(true)
    setError('')
    const ok = await verifyAccessToken(input)
    setSubmitting(false)
    if (ok) {
      accessToken.set(input)
      setUnlocked(true)
    } else {
      setError('Incorrect access code.')
    }
  }

  if (checking) return null
  if (unlocked) return children

  return (
    <div className="access-gate">
      <form className="access-gate-card" onSubmit={handleSubmit}>
        <h1><span className="dot" /> Copy Trading</h1>
        <p>Enter the access code to continue.</p>
        <input
          type="password"
          autoFocus
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Access code"
        />
        {error && <div className="access-gate-error">{error}</div>}
        <button type="submit" className="btn primary" disabled={submitting || !input}>
          {submitting ? 'Checking…' : 'Unlock'}
        </button>
      </form>
    </div>
  )
}
