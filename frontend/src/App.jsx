import { useState, useCallback } from 'react'
import { api } from './api'
import VideoStream from './components/VideoStream.jsx'
import StatsPanel from './components/StatsPanel.jsx'
import PlatesTable from './components/PlatesTable.jsx'
import PlateLog from './components/PlateLog.jsx'
import HistoryPanel from './components/HistoryPanel.jsx'

export default function App() {
  // Defaults to the sample video for local testing - swap for an rtsp:// URL for a
  // real CCTV camera, or "0" for a local webcam.
  const [source, setSource] = useState('highway.mp4')
  const [running, setRunning] = useState(false)
  const [error, setError] = useState(null)
  const [stats, setStats] = useState({})
  const [historyKey, setHistoryKey] = useState(0)

  const handleStats = useCallback((s) => setStats(s), [])

  const start = async () => {
    setRunning(true) // optimistic - blocks a second click from firing before this request resolves
    setError(null)
    try {
      await api.start({ source })
    } catch (e) {
      setRunning(false) // request itself failed (e.g. backend unreachable, bad source) - allow retry
      setError(e.response?.data?.error || 'Could not start analysis. Is the backend running?')
    }
  }

  const stop = async () => {
    await api.stop()
    setRunning(false)
    setHistoryKey((k) => k + 1) // refresh history panel to pick up the session just saved
  }

  return (
    <div className="app">
      <div className="header">
        <div>
          <h1>🚦 AI-Powered CCTV Traffic Analysis</h1>
          <div className="subtitle">Detection • Tracking • Speed • Line Counting • Plate Recognition</div>
        </div>
        <span className={`status-badge ${running ? 'status-live' : 'status-idle'}`}>
          {running ? '● LIVE' : 'IDLE'}
        </span>
      </div>

      <div className="controls">
        <label>
          Camera source (RTSP URL, webcam index, or video file)
          <br />
          <input
            type="text"
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder="rtsp://user:pass@192.168.1.50:554/stream1"
            disabled={running}
          />
        </label>
        {!running ? (
          <button onClick={start}>▶ Start Analysis</button>
        ) : (
          <button className="stop" onClick={stop}>⏹ Stop</button>
        )}
        {error && <span style={{ color: '#f87171', fontSize: 13 }}>{error}</span>}
      </div>

      <div className="grid">
        <div className="panel panel--feed">
          <h2>📹 Live Feed</h2>
          <VideoStream onStats={handleStats} />
        </div>
        <StatsPanel stats={stats} />
      </div>

      <div className="grid">
        <PlatesTable plates={stats.plates} />
        <PlateLog plateLog={stats.plate_log} />
      </div>

      <HistoryPanel refreshKey={historyKey} />
    </div>
  )
}
