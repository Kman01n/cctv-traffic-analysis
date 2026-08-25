import { useEffect, useState } from 'react'
import { api } from '../api'

export default function HistoryPanel({ refreshKey }) {
  const [rows, setRows] = useState([])

  useEffect(() => {
    // Backend returns {"data": [...]}; axios already unwraps the HTTP body into
    // res.data, so the actual row array is one level deeper at res.data.data.
    api.history(10).then((res) => setRows(res.data.data)).catch(() => setRows([]))
  }, [refreshKey])

  return (
    <div className="panel panel--history">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2>📁 Historical Sessions</h2>
        <a href={api.downloadCsvUrl()}>
          <button className="download-btn">⬇ Download CSV</button>
        </a>
      </div>
      {rows.length === 0 ? (
        <p style={{ color: '#6b7280', fontSize: 13 }}>No sessions recorded yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Source</th>
              <th>Frames</th>
              <th>Vehicles</th>
              <th>Crossed</th>
              <th>Avg Speed</th>
              <th>Plates</th>
              <th>When</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.video_name}</td>
                <td>{r.total_frames}</td>
                <td>{r.total_vehicles}</td>
                <td>{r.crossed_vehicles}</td>
                {/* was `r.avg_speed ? ... : '-'`, which treated a genuine 0.0 km/h
                    average as "missing" and showed '-' instead of '0.0' */}
                <td>{r.avg_speed != null ? r.avg_speed.toFixed(1) : '-'}</td>
                <td>{r.plates_read}</td>
                <td>{r.session_timestamp}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
