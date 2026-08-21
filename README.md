# CCTV Traffic Analysis — React + FastAPI

Full-stack version of the vehicle tracking / speed / plate-recognition system, built
for **live CCTV camera feeds** (RTSP), with support for webcams and video files too
(mainly useful for testing without a live camera on hand).

## Architecture

- **backend/detector.py** — the `CCTVDetector` class: YOLO tracking, speed estimation,
  line-crossing counting, and plate OCR. Knows nothing about the web — it just turns
  video frames into `(annotated_frame, stats)` pairs. Can be run standalone
  (`python detector.py`) for testing the CV pipeline alone, no server required.
- **backend/main.py** — the FastAPI app. Loads the models once at startup, runs the
  capture loop on a background thread, and streams annotated frames + live stats to
  the browser over a WebSocket. Exposes REST endpoints for start/stop, history, and
  CSV export.
- **frontend/** — React (Vite) dashboard. Connects to the WebSocket for live video +
  stats, and to the REST API for historical sessions and CSV download.

## What was fixed from the previous version

- **The backend wasn't actually a server.** `main.py` was still the old CLI script
  (`cv2.imshow` + a `while` loop) with no `app = FastAPI()` anywhere, so
  `uvicorn main:app` would fail immediately — there was no `app` to import, and none
  of the endpoints the frontend calls (`/api/start`, `/api/stop`, `/ws/stream`, etc.)
  existed. This is now a real FastAPI app.
- `detector.py` was missing `export_csv()` (needed by the CSV download endpoint) —
  restored.
- Model swapped from `yolov8s.pt` to **`yolov8n.pt`** (nano) for faster CPU inference.
- Added CORS middleware (open for local dev — see the note below before deploying).
- `HistoryPanel.jsx` treated a genuine `0.0` average speed as falsy and displayed `-`
  instead of `0.0` — fixed to check for `null`/`undefined` instead.
- `VideoStream.jsx` now auto-reconnects the WebSocket after a drop (server restart,
  flaky network) instead of leaving the dashboard stuck on a dead connection.
- `main.py`'s `/api/start` now surfaces the actual error (bad RTSP URL, missing file,
  wrong webcam index) back to the dashboard instead of failing silently.

## Running it

### 1. Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

First startup takes a little longer — it downloads `yolov8n.pt` (a few MB) the first
time it's needed, then loads the YOLO, cascade, and OCR models once. The API will be
live at `http://localhost:8000`. Check `/api/status` in a browser to confirm it's running.

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

### 3. Connect a camera

In the dashboard's source field, enter:
- **A live CCTV camera:** `rtsp://username:password@camera-ip:554/stream1` (check your
  camera/NVR's manual for its exact RTSP URL format — this varies by manufacturer)
- **A local webcam:** `0` (or `1`, `2`... if you have more than one)
- **A video file (testing only):** `highway.mp4` (already in `backend/`)

Click **▶ Start Analysis**. The backend opens that source with `cv2.VideoCapture`,
which handles all three cases identically — no code changes needed to switch between
a live camera and a test file.

## Notes on going from "works on my machine" to a real deployment

- **CORS** is wide open (`allow_origins=["*"]`) for local development. Lock this down
  to your actual frontend domain before putting this anywhere reachable from the internet.
- **RTSP reliability**: real CCTV/NVR connections drop frames and occasionally
  disconnect. The frontend now reconnects its WebSocket automatically — for a
  production deployment you'd also want the backend's capture loop to auto-reconnect
  to the camera itself if the RTSP stream drops for an extended period, which isn't
  in this version yet.
- **Multiple cameras**: this version handles one active source at a time. Running
  several cameras simultaneously would mean one `CCTVDetector` + background thread per
  camera, each with its own WebSocket endpoint or a camera ID passed in the stream
  request — a natural next step once single-camera is confirmed working end-to-end.
- **GPU**: `easyocr.Reader(gpu=False)` and no CUDA device is requested for YOLO. If you
  have an NVIDIA GPU available on the server, switching both of those on would speed up
  inference substantially, especially useful once you're running more than one camera.
- **numpy**: `requirements.txt` pins `numpy<2` defensively — some `opencv-python`/
  `easyocr` builds still expect the numpy 1.x ABI and error out under numpy 2.x.
