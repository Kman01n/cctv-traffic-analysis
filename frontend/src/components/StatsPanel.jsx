import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'

export default function StatsPanel({ stats }) {
  const totalDetected = stats.total_detected || {}
  const crossed = stats.crossed || {}
  const totalCount = Object.values(totalDetected).reduce((a, b) => a + b, 0)
  const crossedCount = Object.values(crossed).reduce((a, b) => a + b, 0)

  const chartData = Object.keys(totalDetected).map((cls) => ({
    name: cls,
    detected: totalDetected[cls] || 0,
    crossed: crossed[cls] || 0,
  }))

  return (
    <div className="panel panel--stats">
      <h2>📊 Live Stats</h2>
      <div className="metrics-row">
        <div className="metric">
          <div className="label">Frame</div>
          <div className="value">{stats.frame || 0}</div>
        </div>
        <div className="metric">
          <div className="label">Processing FPS</div>
          <div className="value" style={{
            color: (stats.processing_fps || 0) >= 10 ? '#4ade80'
                 : (stats.processing_fps || 0) >= 5 ? '#facc15' : '#f87171'
          }}>
            {stats.processing_fps || 0}
          </div>
        </div>
        <div className="metric">
          <div className="label">Vehicles Detected</div>
          <div className="value">{totalCount}</div>
        </div>
        <div className="metric">
          <div className="label">Crossed Line</div>
          <div className="value">{crossedCount}</div>
        </div>
        <div className="metric">
          <div className="label">Avg Speed (km/h)</div>
          <div className="value">{stats.avg_speed || 0}</div>
        </div>
      </div>

      {chartData.length > 0 && (
        <ResponsiveContainer width="100%" height={160}>
          <BarChart data={chartData}>
            <XAxis dataKey="name" stroke="#9aa0ac" fontSize={11} />
            <YAxis stroke="#9aa0ac" fontSize={11} allowDecimals={false} />
            <Tooltip contentStyle={{ background: '#161923', border: '1px solid #2a2f3d' }} />
            <Bar dataKey="detected" fill="#3b82f6" radius={[4, 4, 0, 0]} />
            <Bar dataKey="crossed" fill="#f97316" radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}
