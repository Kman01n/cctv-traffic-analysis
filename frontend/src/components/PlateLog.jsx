export default function PlateLog({ plateLog }) {
  if (!plateLog || plateLog.length === 0) {
    return (
      <div className="panel">
        <h2>📋 Plate Log (All Detected This Session)</h2>
        <p style={{ color: '#6b7280', fontSize: 13 }}>
          No plates locked yet — entries appear here permanently once read, and stay even
          after the vehicle passes out of frame.
        </p>
      </div>
    )
  }

  return (
    <div className="panel">
      <h2>📋 Plate Log (All Detected This Session)</h2>
      <div style={{ maxHeight: 300, overflowY: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>Vehicle ID</th>
              <th>Class</th>
              <th>Plate</th>
              <th>Confidence</th>
              <th>Frame</th>
            </tr>
          </thead>
          <tbody>
            {plateLog.map((entry, i) => (
              <tr key={`${entry.track_id}-${entry.frame}-${i}`}>
                <td>{entry.track_id}</td>
                <td>{entry.vehicle_class}</td>
                <td>{entry.plate_text}</td>
                <td>{entry.confidence}</td>
                <td>{entry.frame}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
