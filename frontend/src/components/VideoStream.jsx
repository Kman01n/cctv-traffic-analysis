import { useEffect, useRef, useState } from 'react'
import { WS_URL } from '../api'

export default function VideoStream({ onStats }) {
  const [frameSrc, setFrameSrc] = useState(null)
  const [connected, setConnected] = useState(false)
  const wsRef = useRef(null)
  const reconnectTimerRef = useRef(null)
  const unmountedRef = useRef(false)

  useEffect(() => {
    unmountedRef.current = false

    const connect = () => {
      const ws = new WebSocket(WS_URL)
      wsRef.current = ws

      ws.onopen = () => setConnected(true)

      ws.onmessage = (event) => {
        const data = JSON.parse(event.data)
        if (data.type === 'frame') {
          setFrameSrc(`data:image/jpeg;base64,${data.frame}`)
          onStats(data.stats)
        }
      }

      // A dropped RTSP/CCTV connection on the backend, a server restart, or a flaky
      // network shouldn't require the user to refresh the page - reconnect after a
      // short delay as long as the component is still mounted.
      const handleDisconnect = () => {
        setConnected(false)
        if (!unmountedRef.current) {
          reconnectTimerRef.current = setTimeout(connect, 2000)
        }
      }
      ws.onclose = handleDisconnect
      ws.onerror = handleDisconnect
    }

    connect()

    return () => {
      unmountedRef.current = true
      clearTimeout(reconnectTimerRef.current)
      wsRef.current?.close()
    }
  }, [onStats])

  if (!connected) {
    return <div className="video-placeholder">Connecting to camera stream...</div>
  }

  if (!frameSrc) {
    return <div className="video-placeholder">Waiting for video - click Start Analysis</div>
  }

  return <img className="video-frame" src={frameSrc} alt="Live CCTV feed" />
}
