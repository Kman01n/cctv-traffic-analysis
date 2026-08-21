"""
CCTVDetector - the full detection/tracking/speed/plate pipeline.

This file is intentionally server-agnostic: it knows nothing about FastAPI,
WebSockets, or HTTP. main.py is the only thing that talks to the outside world;
this class just opens a video source and turns frames into (annotated_frame, stats)
pairs. That split means the CV pipeline can be tested/run standalone (see the
__main__ block at the bottom) without spinning up a web server.

Key design points:
  - iou/classes tuning so one physical vehicle doesn't produce two overlapping boxes
  - plate confidence floor + locking (stop re-scanning once a plate is confidently read)
  - same-vehicle merge when the tracker re-assigns a new ID to a vehicle it already knows
  - plate OCR (cascade + EasyOCR) runs on a background thread pool so it never blocks
    the video loop - OCR is the slowest step (100-500ms+ per call on CPU)
  - a threading.Lock guards every piece of state touched by both the main loop and
    the background OCR workers (detected_plates, plate_log, counts, DB cursor)
"""

import csv
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import cv2


class CCTVDetector:
    """
    Full detection/tracking/speed/plate pipeline, generalized to work with ANY camera
    source: a live RTSP CCTV stream ("rtsp://..."), a local webcam (0, 1, ...), or a
    video file path (used for testing/training, not the primary live-CCTV use case).
    """

    def __init__(self, source, model, plate_cascade, reader, db_path="traffic_data.db",
                 ocr_interval=2, plate_min_confidence=0.45, plate_lock_confidence=0.60,
                 line_position=0.6):
        self.source = source
        self.db_path = db_path
        self.ocr_interval = ocr_interval
        self.plate_min_confidence = plate_min_confidence
        self.plate_lock_confidence = plate_lock_confidence
        self.line_position = line_position

        # Passed in already-loaded so starting/stopping sessions is fast - the
        # expensive one-time cost happens once at server startup, not on every
        # click of "Start Analysis."
        self.model = model
        self.plate_cascade = plate_cascade
        self.reader = reader

        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.cursor = self.conn.cursor()
        self._init_db()

        self.cap = None
        self.frame_width = 0
        self.frame_height = 0
        self.fps_video = 25
        self.line_y = 0
        self.meters_per_pixel = 0

        self.frame_count = 0
        self.unique_ids_seen = set()
        self.total_class_counts = {}
        self.locked_class = {}
        self.class_crossed_counts = {}
        self.counted_ids = set()
        self.prev_positions = {}
        self.speed_estimates = {}
        self.detected_plates = {}
        self.plate_to_canonical_id = {}
        self.plate_log = []
        self.track_id_aliases = {}

        self.min_vehicle_width_px = 90
        self.min_vehicle_height_px = 60

        self.running = False

        # --- Async OCR pipeline ---
        # Plate reading (cascade + EasyOCR) is the single slowest step in the whole
        # pipeline, often 100-500ms+ per call on CPU. Running it inline in the frame
        # loop is what makes video choppy - every OCR call would stall frame output.
        # Submitting it to a background pool lets video keep flowing while plate
        # reads complete whenever they complete.
        self.ocr_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="plate-ocr")
        # Guards every piece of state that both the main frame loop AND the background
        # OCR workers touch (detected_plates, plate_log, counts, DB cursor).
        self.state_lock = threading.Lock()
        # Track_ids currently being OCR'd by a background worker, so a vehicle doesn't
        # get submitted twice while its first job is still running.
        self.ocr_pending = set()

        # Smoothed real processing FPS - reflects actual frame throughput (detection +
        # draw), decoupled from the WebSocket's fixed push rate to the browser.
        self.processing_fps = 0.0

    def _init_db(self):
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER,
                vehicle_class TEXT,
                frame_number INTEGER,
                crossed_line INTEGER,
                speed_kph REAL,
                plate_text TEXT,
                plate_confidence REAL,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.cursor.execute("PRAGMA table_info(detections)")
        existing = [c[1] for c in self.cursor.fetchall()]
        for col, typ in [("speed_kph", "REAL"), ("plate_text", "TEXT"), ("plate_confidence", "REAL")]:
            if col not in existing:
                self.cursor.execute(f"ALTER TABLE detections ADD COLUMN {col} {typ}")

        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS traffic_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_name TEXT,
                total_frames INTEGER,
                total_vehicles INTEGER,
                crossed_vehicles INTEGER,
                avg_speed REAL,
                plates_read INTEGER,
                session_timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.cursor.execute("PRAGMA table_info(traffic_stats)")
        existing = [c[1] for c in self.cursor.fetchall()]
        if "avg_speed" not in existing:
            self.cursor.execute("ALTER TABLE traffic_stats ADD COLUMN avg_speed REAL")
        if "plates_read" not in existing:
            self.cursor.execute("ALTER TABLE traffic_stats ADD COLUMN plates_read INTEGER")
        self.conn.commit()

    def open(self):
        self.cap = cv2.VideoCapture(self.source)
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open video source: {self.source}")

        self.frame_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.frame_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.fps_video = self.cap.get(cv2.CAP_PROP_FPS) or 25
        self.line_y = int(self.frame_height * self.line_position)
        self.meters_per_pixel = 30 / self.frame_height if self.frame_height else 0
        self.running = True

    def close(self):
        self.running = False
        if self.cap is not None:
            self.cap.release()
        # wait=True: let in-flight OCR jobs finish (they still touch self.conn) before
        # we close the connection below; cancel_futures drops anything not yet started.
        self.ocr_executor.shutdown(wait=True, cancel_futures=True)
        self._save_session_summary()
        self.conn.close()

    def read_frame(self):
        if self.cap is None or not self.cap.isOpened():
            return None, None

        frame_start_time = time.time()
        success, frame = self.cap.read()
        if not success:
            return None, None

        self.frame_count += 1
        process_plates = (self.frame_count % self.ocr_interval == 0)

        # classes=[2,5,7] restricts YOLO to car/bus/truck (COCO ids), so a single vehicle
        # never gets flagged under two different classes producing two boxes.
        # iou=0.5 (tighter than the 0.7 default) makes NMS merge overlapping boxes more
        # aggressively, so one physical vehicle doesn't survive as two separate detections.
        results = self.model.track(frame, persist=True, verbose=False, conf=0.25,
                                    iou=0.5, imgsz=960, classes=[2, 5, 7])
        boxes = results[0].boxes
        annotated_frame = frame.copy()

        if boxes.id is not None:
            active_ids = [int(tid) for tid in boxes.id]

            # Purge plate memory for vehicles that have left the frame
            with self.state_lock:
                for reg_id in list(self.detected_plates.keys()):
                    if reg_id not in active_ids:
                        del self.detected_plates[reg_id]

            for box, track_id in zip(boxes, boxes.id):
                track_id = int(track_id)
                # If this ID was previously proven to be the same vehicle as an earlier
                # track_id (matched by plate text), always resolve to that canonical ID.
                track_id = self.track_id_aliases.get(track_id, track_id)
                cls_id = int(box.cls[0])
                cls_name_now = self.model.names[cls_id]

                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cx, cy = int((x1 + x2) / 2), int((y1 + y2) / 2)

                # --- Lock class on first detection ---
                if track_id not in self.locked_class:
                    with self.state_lock:
                        if track_id not in self.locked_class:  # re-check inside lock
                            self.locked_class[track_id] = cls_name_now
                            self.unique_ids_seen.add(track_id)
                            self.total_class_counts[cls_name_now] = self.total_class_counts.get(cls_name_now, 0) + 1
                            self.cursor.execute(
                                "INSERT INTO detections (track_id, vehicle_class, frame_number, crossed_line, "
                                "speed_kph, plate_text, plate_confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (track_id, cls_name_now, self.frame_count, 0, 0, None, None)
                            )
                            self.conn.commit()

                cls_name = self.locked_class[track_id]

                # --- Speed estimation ---
                if track_id in self.prev_positions:
                    prev_cx, prev_cy, prev_frame = self.prev_positions[track_id]
                    pixel_distance = ((cx - prev_cx) ** 2 + (cy - prev_cy) ** 2) ** 0.5
                    frame_gap = self.frame_count - prev_frame
                    if frame_gap == 1:
                        time_elapsed = frame_gap / self.fps_video
                        real_distance_m = pixel_distance * self.meters_per_pixel
                        speed_kph = (real_distance_m / time_elapsed) * 3.6
                        if speed_kph <= 150:
                            if track_id in self.speed_estimates:
                                speed_kph = 0.6 * self.speed_estimates[track_id] + 0.4 * speed_kph
                            self.speed_estimates[track_id] = speed_kph

                # --- Line crossing ---
                prev_cy_for_crossing = self.prev_positions[track_id][1] if track_id in self.prev_positions else None
                self.prev_positions[track_id] = (cx, cy, self.frame_count)
                speed = self.speed_estimates.get(track_id, 0)

                just_crossed = False
                if prev_cy_for_crossing is not None:
                    if track_id not in self.counted_ids and (
                        (prev_cy_for_crossing < self.line_y <= cy) or (prev_cy_for_crossing > self.line_y >= cy)
                    ):
                        self.counted_ids.add(track_id)
                        self.class_crossed_counts[cls_name] = self.class_crossed_counts.get(cls_name, 0) + 1
                        just_crossed = True
                        with self.state_lock:
                            plate_at_crossing = self.detected_plates.get(track_id, {}).get("text")
                            self.cursor.execute(
                                "UPDATE detections SET crossed_line = 1, speed_kph = ?, "
                                "plate_text = COALESCE(?, plate_text) WHERE track_id = ?",
                                (speed, plate_at_crossing, track_id)
                            )
                            self.conn.commit()

                # --- Submit plate OCR to the background pool (never blocks this loop) ---
                vh, vw = y2 - y1, x2 - x1
                vehicle_crop = frame[max(0, y1):y2, max(0, x1):x2]

                with self.state_lock:
                    already_locked = self.detected_plates.get(track_id, {}).get("locked", False)
                    already_pending = track_id in self.ocr_pending

                if (process_plates and not already_locked and not already_pending
                        and vehicle_crop.size > 0
                        and vw >= self.min_vehicle_width_px and vh >= self.min_vehicle_height_px):
                    with self.state_lock:
                        self.ocr_pending.add(track_id)
                    # .copy() is important: vehicle_crop is a view into `frame`, which
                    # gets overwritten on the next loop iteration. Without copying, the
                    # background worker could end up reading pixels from a totally
                    # different, later frame by the time it actually runs.
                    self.ocr_executor.submit(self._read_plate_worker, track_id, vehicle_crop.copy(), vh, vw)

                # --- Visualization: speed-colored box ---
                color = (0, 255, 0) if speed < 40 else (0, 255, 255) if speed < 80 else (0, 0, 255)
                cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), color, 2)
                label = f"ID:{track_id} {cls_name} {speed:.0f}km/h"
                (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
                cv2.rectangle(annotated_frame, (x1, y1 - th - 8), (x1 + tw + 4, y1), color, -1)
                cv2.putText(annotated_frame, label, (x1 + 2, y1 - 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
                cv2.circle(annotated_frame, (cx, cy), 3, (0, 0, 255), -1)
                if just_crossed:
                    cv2.circle(annotated_frame, (cx, cy), 12, (0, 165, 255), 2)

                with self.state_lock:
                    plate_data_for_draw = self.detected_plates.get(track_id)
                if plate_data_for_draw:
                    v_px, v_py, v_pw, v_ph = plate_data_for_draw["coords"]
                    cv2.rectangle(annotated_frame, (x1 + v_px, y1 + v_py),
                                  (x1 + v_px + v_pw, y1 + v_py + v_ph), (255, 0, 255), 2)
                    cv2.putText(annotated_frame, f"Plate: {plate_data_for_draw['text']}", (x1, y2 + 25),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 255), 2)

        # --- Counting line ---
        cv2.line(annotated_frame, (0, self.line_y), (self.frame_width, self.line_y), (0, 0, 255), 2)
        cv2.putText(annotated_frame, "COUNT LINE", (10, self.line_y - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

        elapsed = time.time() - frame_start_time
        if elapsed > 0:
            instant_fps = 1 / elapsed
            self.processing_fps = (0.9 * self.processing_fps + 0.1 * instant_fps
                                    if self.processing_fps else instant_fps)

        stats = {
            "frame": self.frame_count,
            "processing_fps": round(self.processing_fps, 1),
            "total_detected": self.total_class_counts,
            "crossed": self.class_crossed_counts,
            "avg_speed": round(
                sum(self.speed_estimates.values()) / len(self.speed_estimates), 1
            ) if self.speed_estimates else 0,
            "plates": [
                {"track_id": tid, "text": p["text"], "confidence": round(p["confidence"], 2),
                 "locked": p.get("locked", False)}
                for tid, p in self.detected_plates.items()
            ],
            "plate_log": list(reversed(self.plate_log[-200:])),
        }
        self.conn.commit()
        return annotated_frame, stats

    def _read_plate_worker(self, track_id, vehicle_crop, vh, vw):
        """Runs on a background thread pool. The cascade + EasyOCR call below touches
        no shared state, so it's safe to run fully in parallel with the main frame loop
        and other OCR workers - only the final 'apply the result' section needs the lock."""
        try:
            gray = cv2.cvtColor(vehicle_crop, cv2.COLOR_BGR2GRAY)
            plates = self.plate_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 14))

            ocr_target_crop = None
            v_px = v_py = v_pw = v_ph = 0
            if len(plates) > 0:
                (v_px, v_py, v_pw, v_ph) = plates[0]
                aspect_ratio = v_pw / max(v_ph, 1)
                if 2.0 <= aspect_ratio <= 6.0:
                    ocr_target_crop = vehicle_crop[v_py:v_py + v_ph, v_px:v_px + v_pw]

            if ocr_target_crop is None:
                v_py = int(vh * 0.55)
                v_px = int(vw * 0.1)
                v_pw = int(vw * 0.8)
                v_ph = int(vh * 0.4)
                ocr_target_crop = vehicle_crop[v_py:v_py + v_ph, v_px:v_px + v_pw]

            if ocr_target_crop is None or ocr_target_crop.size == 0:
                return

            crop_h, crop_w = ocr_target_crop.shape[:2]
            if crop_w < 150:
                scale = 150 / crop_w
                ocr_target_crop = cv2.resize(
                    ocr_target_crop, (int(crop_w * scale), int(crop_h * scale)),
                    interpolation=cv2.INTER_CUBIC
                )

            ocr_result = self.reader.readtext(
                ocr_target_crop,
                allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            )
            if not ocr_result:
                return

            # Sort left-to-right by x-coordinate so multi-segment plates read correctly
            ocr_result.sort(key=lambda r: r[0][0][0])
            plate_text = "".join([res[1] for res in ocr_result]).strip()
            avg_conf = float(sum(res[2] for res in ocr_result) / len(ocr_result))

            has_letter = any(c.isalpha() for c in plate_text)
            has_digit = any(c.isdigit() for c in plate_text)
            plausible_plate = (4 <= len(plate_text) <= 10) and has_letter and has_digit

            if not (plausible_plate and avg_conf >= self.plate_min_confidence):
                return

            # --- Everything past this point touches shared state - lock required ---
            with self.state_lock:
                existing = self.detected_plates.get(track_id)
                if existing is not None and avg_conf <= existing["confidence"]:
                    return

                is_locked = bool(avg_conf >= self.plate_lock_confidence)
                norm_plate = plate_text.upper().replace(" ", "")

                # --- Same-vehicle merge check ---
                # If this plate was already locked under a DIFFERENT track_id, the
                # tracker almost certainly lost and re-acquired the same physical
                # vehicle with a new ID. Merge this ID into the original one instead
                # of recording it as a second vehicle.
                if (is_locked and norm_plate in self.plate_to_canonical_id
                        and self.plate_to_canonical_id[norm_plate] != track_id):
                    canonical_id = self.plate_to_canonical_id[norm_plate]

                    # Undo the vehicle count that was wrongly added when this new
                    # track_id first appeared, and remove its duplicate DB row.
                    if track_id in self.locked_class:
                        dup_cls = self.locked_class[track_id]
                        self.total_class_counts[dup_cls] = max(0, self.total_class_counts.get(dup_cls, 0) - 1)
                        del self.locked_class[track_id]
                    self.unique_ids_seen.discard(track_id)
                    self.cursor.execute("DELETE FROM detections WHERE track_id = ?", (track_id,))

                    # From this point on, this raw ID always resolves to the canonical one
                    self.track_id_aliases[track_id] = canonical_id
                    self.detected_plates.pop(track_id, None)
                    self.conn.commit()
                else:
                    self.plate_to_canonical_id.setdefault(norm_plate, track_id)
                    self.detected_plates[track_id] = {
                        "text": plate_text, "confidence": avg_conf,
                        "coords": (v_px, v_py, v_pw, v_ph), "locked": is_locked
                    }
                    self.cursor.execute(
                        "UPDATE detections SET plate_text = ?, plate_confidence = ? WHERE track_id = ?",
                        (plate_text, avg_conf, track_id)
                    )
                    self.conn.commit()

                    if is_locked:
                        self.plate_log.append({
                            "track_id": track_id,
                            "vehicle_class": self.locked_class.get(track_id, "unknown"),
                            "plate_text": plate_text,
                            "confidence": round(avg_conf, 2),
                            "frame": self.frame_count,
                        })
        finally:
            # Always clear the pending flag, even on error, so this vehicle isn't
            # permanently stuck un-retryable if something above raised.
            with self.state_lock:
                self.ocr_pending.discard(track_id)

    def export_csv(self, filename=None):
        """Writes every row in the detections table out to a CSV report and returns the filename.

        (This was present in the old single-file main.py but had been dropped when the
        pipeline was split out into this file - restored here since main.py's
        /api/download-csv endpoint needs it.)
        """
        if filename is None:
            filename = f"traffic_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        self.cursor.execute(
            "SELECT track_id, vehicle_class, frame_number, crossed_line, speed_kph, "
            "plate_text, plate_confidence, timestamp FROM detections"
        )
        rows = self.cursor.fetchall()
        with open(filename, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Track ID", "Vehicle Class", "Frame Number", "Crossed Line",
                              "Speed (km/h)", "Plate Text", "Plate Confidence", "Timestamp"])
            writer.writerows(rows)
        return filename

    def _save_session_summary(self):
        avg_speed = sum(self.speed_estimates.values()) / len(self.speed_estimates) if self.speed_estimates else 0
        plates_read = len(self.detected_plates)
        self.cursor.execute(
            "INSERT INTO traffic_stats (video_name, total_frames, total_vehicles, crossed_vehicles, "
            "avg_speed, plates_read) VALUES (?, ?, ?, ?, ?, ?)",
            (str(self.source), self.frame_count, len(self.unique_ids_seen), len(self.counted_ids),
             avg_speed, plates_read)
        )
        self.conn.commit()


# ================== STANDALONE TEST MODE ==================
# Lets you sanity-check the pipeline itself (no web server, no browser) with:
#   python detector.py
if __name__ == "__main__":
    import easyocr
    from ultralytics import YOLO

    VIDEO_SOURCE = "highway.mp4"

    print("--> Loading YOLOv8n model, plate cascade, and OCR reader...")
    yolo_model = YOLO("yolov8n.pt")
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_russian_plate_number.xml")
    ocr_reader = easyocr.Reader(['en'], gpu=False)

    detector = CCTVDetector(VIDEO_SOURCE, yolo_model, cascade, ocr_reader)

    try:
        detector.open()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)

    print("Video opened successfully. Running pipeline standalone... (press 'q' to quit)")
    window_name = "CCTVDetector standalone test"
    try:
        while detector.running:
            annotated_frame, stats = detector.read_frame()
            if annotated_frame is None:
                print("Finished reading video.")
                break
            cv2.imshow(window_name, annotated_frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        detector.close()
        cv2.destroyAllWindows()

    csv_filename = detector.export_csv()
    print(f"Report exported to: {csv_filename}")
    print("All data saved to traffic_data.db")
