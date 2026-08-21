"""
AI-Powered CCTV Traffic Analysis - FastAPI backend.

This file ONLY wires up the web server: HTTP endpoints, the WebSocket stream, and
the background capture thread. All the actual detection/tracking/speed/plate logic
lives in detector.py's CCTVDetector class - keeping this file lean means adding a
second camera endpoint, auth, etc. later doesn't mean touching the CV pipeline.

Run with:
    uvicorn main:app --reload --port 8000
"""

import asyncio
import base64
import csv
import os
import sqlite3
import tempfile
import threading
import time
from typing import Optional

import cv2
import easyocr
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from ultralytics import YOLO

from detector import CCTVDetector

DB_PATH = "traffic_data.db"

# ---------------------------------------------------------------------------
# Models are loaded ONCE, here, at server startup - not on every "Start Analysis"
# click (that's what made the old single-file version slow to start each run).
#
# yolov8n.pt (nano) instead of yolov8s.pt (small): the nano model is roughly
# 3-4x faster on CPU with only a modest accuracy trade-off. That matters a lot
# here since there's no GPU in the loop by default - see the README note on
# turning GPU on if you have an NVIDIA card available.
# ---------------------------------------------------------------------------
print("--> Loading YOLOv8n model, plate cascade, and OCR reader (once, at startup)...")
yolo_model = YOLO("yolov8n.pt")
plate_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_russian_plate_number.xml")
ocr_reader = easyocr.Reader(["en"], gpu=False)
print("--> Models loaded. Server ready.")

app = FastAPI(title="CCTV Traffic Analysis API")

# Wide open for local development. Lock this down to your actual frontend
# origin before putting this anywhere reachable from the internet.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class StartRequest(BaseModel):
    source: str


class StreamState:
    """Thread-safe holder for the latest annotated frame + stats.

    The capture thread (below) writes to this as fast as the CV pipeline can go.
    The WebSocket loop reads from it at a fixed rate regardless of how fast
    detection is actually running, so a slow model just means a stiller-looking
    video rather than a blocked or broken one.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.frame_bytes: Optional[bytes] = None
        self.stats: dict = {}

    def set(self, frame_bytes, stats):
        with self.lock:
            self.frame_bytes = frame_bytes
            self.stats = stats

    def get(self):
        with self.lock:
            return self.frame_bytes, self.stats


state = StreamState()

detector: Optional[CCTVDetector] = None
capture_thread: Optional[threading.Thread] = None
stop_event = threading.Event()
last_error: Optional[str] = None


def _capture_loop(source: str):
    """Runs on a background thread: opens the source and pulls frames as fast as
    the pipeline can process them, publishing each result into `state`."""
    global detector, last_error
    try:
        detector = CCTVDetector(source, yolo_model, plate_cascade, ocr_reader, db_path=DB_PATH)
        detector.open()
    except RuntimeError as e:
        last_error = str(e)
        detector = None
        return

    while not stop_event.is_set() and detector.running:
        annotated_frame, stats = detector.read_frame()
        if annotated_frame is None:
            break
        ok, buf = cv2.imencode(".jpg", annotated_frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            state.set(buf.tobytes(), stats)

    if detector is not None:
        detector.close()


@app.post("/api/start")
def start_analysis(req: StartRequest):
    global capture_thread, last_error

    if capture_thread is not None and capture_thread.is_alive():
        return JSONResponse(status_code=409, content={"error": "Analysis already running. Stop it first."})

    last_error = None
    stop_event.clear()
    state.set(None, {})
    capture_thread = threading.Thread(target=_capture_loop, args=(req.source,), daemon=True)
    capture_thread.start()

    # Give the capture thread a brief moment to fail fast (bad RTSP URL, missing
    # file, wrong webcam index) so /api/start can report the real error instead
    # of a false "started" the frontend has to discover was wrong later.
    time.sleep(0.5)
    if last_error:
        return JSONResponse(status_code=400, content={"error": last_error})

    return {"status": "started", "source": req.source}


@app.post("/api/stop")
def stop_analysis():
    global capture_thread
    stop_event.set()
    if capture_thread is not None:
        capture_thread.join(timeout=10)
    capture_thread = None
    return {"status": "stopped"}


@app.get("/api/status")
def status():
    running = capture_thread is not None and capture_thread.is_alive()
    return {"running": running, "error": last_error}


@app.get("/api/history")
def history(limit: int = 10):
    # Own short-lived connection so history works even when no analysis is
    # currently running (the detector's connection only exists while active).
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM traffic_stats ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return {"data": [dict(r) for r in rows]}


@app.get("/api/detections")
def detections(limit: int = 100):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM detections ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return {"data": [dict(r) for r in rows]}


@app.get("/api/download-csv")
def download_csv():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT track_id, vehicle_class, frame_number, crossed_line, speed_kph, "
        "plate_text, plate_confidence, timestamp FROM detections"
    ).fetchall()
    conn.close()

    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Track ID", "Vehicle Class", "Frame Number", "Crossed Line",
                          "Speed (km/h)", "Plate Text", "Plate Confidence", "Timestamp"])
        writer.writerows(rows)

    return FileResponse(path, filename="traffic_report.csv", media_type="text/csv")


@app.websocket("/ws/stream")
async def ws_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            frame_bytes, stats = state.get()
            if frame_bytes is not None:
                await websocket.send_json({
                    "type": "frame",
                    "frame": base64.b64encode(frame_bytes).decode("ascii"),
                    "stats": stats,
                })
            await asyncio.sleep(1 / 12)  # fixed ~12fps push rate to the browser
    except WebSocketDisconnect:
        pass
