import axios from 'axios'

// Read from environment variables, fallback to localhost if missing
const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'
export const WS_URL = import.meta.env.VITE_WS_URL || 'ws://localhost:8000/ws/stream'

export const api = {
  start: (params) => axios.post(`${API_BASE}/api/start`, params),
  stop: () => axios.post(`${API_BASE}/api/stop`),
  status: () => axios.get(`${API_BASE}/api/status`),
  history: (limit = 10) => axios.get(`${API_BASE}/api/history?limit=${limit}`),
  detections: (limit = 100) => axios.get(`${API_BASE}/api/detections?limit=${limit}`),
  downloadCsvUrl: () => `${API_BASE}/api/download-csv`,
}
