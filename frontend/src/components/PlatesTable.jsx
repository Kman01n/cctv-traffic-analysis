export default function PlatesTable({ plates }) {
  if (!plates || plates.length === 0) {
    return (
      <div className="panel panel--plates">
        <h2>🚘 Visible Plates</h2>
        <p style={{ color: '#6b7280', fontSize: 13 }}>No plates read yet.</p>
      </div>
    )
  }

  return (
    <div className="panel panel--plates">
      <h2>🚘 Visible Plates</h2>
      <table>
        <thead>
          <tr>
            <th>Vehicle ID</th>
            <th>Plate</th>
            <th>Confidence</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {plates.map((p) => (
            <tr key={p.track_id}>
              <td>{p.track_id}</td>
              <td>{p.text}</td>
              <td>{p.confidence}</td>
              <td className={p.locked ? 'badge-locked' : 'badge-reading'}>
                {p.locked ? 'LOCKED' : 'reading'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
