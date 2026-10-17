"""Render real YOLO detections beside the existing server's parking-state output.

The supplied checkpoint and source video are required. All boxes, counts and
space states are produced by inference; this script never supplies mock labels.
"""

import argparse
import hashlib
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import tempfile
from datetime import datetime, timezone

import cv2
import imageio_ffmpeg
import numpy as np
import torch
import ultralytics

ROOT = Path(__file__).resolve().parents[1]
COLORS = {"occupied": (77, 82, 225), "empty": (94, 162, 27)}
INK = (58, 45, 30)
MUTED = (118, 103, 84)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_report(path, report):
    # Keep metadata easy to read while storing one complete observation per line.
    summary = {k: v for k, v in report.items() if k != "observations"}
    header = json.dumps(summary, ensure_ascii=False, indent=2)[:-1].rstrip()
    rows = ["    " + json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            for row in report["observations"]]
    path.write_text(header + ',\n  "observations": [\n' + ",\n".join(rows) + "\n  ]\n}\n")


def text(frame, value, point, scale=.55, color=INK, thickness=1):
    cv2.putText(frame, value, point, cv2.FONT_HERSHEY_SIMPLEX, scale,
                color, thickness, cv2.LINE_AA)


def render(frame, detections, spaces, states, title, source_time, frame_skip, model_label):
    result = np.full((680, 1440, 3), (251, 249, 246), dtype=np.uint8)
    text(result, "BINJARI / ACTUAL MODEL INFERENCE", (32, 35), .55, (166, 105, 38), 2)
    text(result, title, (32, 81), 1.1, INK, 2)
    text(result, f"{model_label} | conf >= 0.50", (880, 45), .6, MUTED)
    text(result, "Recorded campus video / CPU inference", (880, 77), .55, MUTED)
    text(result, "01  MODEL DETECTIONS", (32, 126), .65, (166, 105, 38), 2)
    text(result, "02  SERVER PARKING STATES", (736, 126), .65, (103, 125, 24), 2)
    h, w = frame.shape[:2]
    scale = min(672 / w, 440 / h)
    rw, rh = int(w * scale), int(h * scale)
    top = 152 + (440 - rh) // 2
    panels = []
    for left in (32, 736):
        cv2.rectangle(result, (left, 152), (left + 672, 592), (235, 232, 225), -1)
        image = cv2.resize(frame, (rw, rh), interpolation=cv2.INTER_AREA)
        panels.append((image, left + (672 - rw) // 2))

    raw, raw_left = panels[0]
    for detection in detections:
        status = "empty" if detection["class_id"] == 0 else "occupied"
        color = COLORS[status]
        x1, y1, x2, y2 = [int(v * scale) for v in detection["xyxy"]]
        cv2.rectangle(raw, (x1, y1), (x2, y2), color, 2)
        label = f"{'E' if status == 'empty' else 'O'} {detection['confidence']:.2f}"
        label_y = max(13, y1)
        cv2.rectangle(raw, (x1, label_y - 13), (min(rw, x1 + 61), label_y), color, -1)
        text(raw, label, (x1 + 2, label_y - 3), .33, (255, 255, 255))

    stable, stable_left = panels[1]
    tint = stable.copy()
    for space in spaces:
        state = states[space["id"]]["status"]
        points = (np.asarray(space["coords"], dtype=float) * scale).astype(np.int32)
        cv2.fillPoly(tint, [points], COLORS[state])
    stable[:] = cv2.addWeighted(stable, .87, tint, .13, 0)
    for space in spaces:
        state = states[space["id"]]["status"]
        points = (np.asarray(space["coords"], dtype=float) * scale).astype(np.int32)
        cv2.polylines(stable, [points], True, COLORS[state], 2, cv2.LINE_AA)
        x, y = points.mean(axis=0).astype(int)
        label = space["id"].replace("disabled", "").replace("electric", "")
        label_width = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, .39, 1)[0][0]
        cv2.rectangle(stable, (x - label_width // 2 - 3, y - 9),
                      (x + label_width // 2 + 3, y + 7), (255, 255, 255), -1)
        text(stable, label, (x - label_width // 2, y + 4), .39, INK)
    result[top:top + rh, raw_left:raw_left + rw] = raw
    result[top:top + rh, stable_left:stable_left + rw] = stable
    raw_empty = sum(d["class_id"] == 0 for d in detections)
    occupied = sum(s["status"] == "occupied" for s in states.values())
    text(result, f"Boxes: {len(detections) - raw_empty} occupied / {raw_empty} empty",
         (32, 623), .62, INK, 2)
    text(result, f"Spaces: {occupied} occupied / {len(spaces) - occupied} empty / {len(spaces)} total",
         (736, 623), .62, INK, 2)
    text(result, f"SOURCE {source_time:05.2f}s  |  infer every {frame_skip} source frames  |  E = empty, O = occupied",
         (32, 661), .5, MUTED)
    text(result, "Filter + state machine: app_v1.py", (965, 661), .48, MUTED)
    return result


class RecordedModel:
    def __init__(self, model, device):
        self.model, self.device, self.latest = model, device, None

    def __call__(self, frame, conf):
        self.latest = self.model.predict(frame, conf=conf, imgsz=640,
                                         device=self.device, verbose=False)[0]
        return [self.latest]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--lot", choices=["parking_lot_A", "parking_lot_B"], required=True)
    parser.add_argument("--output", type=Path, required=True, help="Output prefix (no extension)")
    parser.add_argument("--start", type=float, default=3, help="Export start in source-video seconds")
    parser.add_argument("--duration", type=float, default=8)
    parser.add_argument("--warmup", type=float, default=3, help="Preceding source seconds used to settle filters")
    parser.add_argument("--frame-skip", type=int, default=5)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    if args.frame_skip < 1 or args.duration <= 0 or args.start < 0 or args.warmup < 0:
        parser.error("frame-skip/duration must be positive; start/warmup must be nonnegative")
    args.model = args.model.resolve(strict=True)
    args.video = args.video.resolve(strict=True)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(args.threads)
    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open source video: {args.video.name}")
    fps, total = cap.get(cv2.CAP_PROP_FPS), int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if fps <= 0 or (args.start + args.duration) * fps > total:
        raise ValueError("The requested segment exceeds the source video duration")
    source_size = [int(cap.get(3)), int(cap.get(4))]
    first_frame = int(max(0, args.start - args.warmup) * fps)
    start_frame = math.ceil(args.start * fps)
    end_frame = math.ceil((args.start + args.duration) * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, first_frame)
    writer = imageio_ffmpeg.write_frames(str(output.with_suffix(".mp4")), (1440, 680),
        pix_fmt_in="bgr24", fps=fps / args.frame_skip, codec="libx264", quality=None,
        macro_block_size=1, output_params=["-crf", "23", "-movflags", "+faststart"])
    writer.send(None)
    observations, inference_ms = [], []
    title = "BUILDING 55 PARKING" if args.lot == "parking_lot_A" else "LIBRARY PARKING"
    exported = 0

    # Import the application without starting its HTTP server or GUI. A temporary
    # DB and log keep this replay separate from operational parking records.
    with tempfile.TemporaryDirectory(prefix="binjari-demo-") as temp:
        original_cwd = Path.cwd()
        os.environ["MODEL_PATH"] = str(args.model)
        os.environ["DB_PATH"] = str(Path(temp) / "replay.db")
        os.chdir(temp)
        try:
            spec = importlib.util.spec_from_file_location("binjari_demo_server", ROOT / "app_v1.py")
            app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(app)
            app.logger.setLevel(logging.ERROR)
            app.init_db()
            system = app.ParkingSystem(show_video=False)
            trained_model = system.model
            if trained_model.names != {0: "space-empty", 1: "space-occupied"}:
                raise ValueError("The server requires the custom space-empty/space-occupied checkpoint")
            parameter_count = sum(p.numel() for p in trained_model.model.parameters())
            model_label = Path(trained_model.ckpt.get("train_args", {}).get("model", args.model.name)).stem
            recorder = RecordedModel(trained_model, args.device)
            system.model = recorder
            connection = app.sqlite3.connect(os.environ["DB_PATH"])
            try:
                for index in range(first_frame, end_frame):
                    ok, frame = cap.read()
                    if not ok:
                        raise RuntimeError(f"Video decoding ended unexpectedly at source frame {index}")
                    if (index + 1) % args.frame_skip:
                        continue
                    states = system._detect_vehicles(frame, args.lot, connection)
                    prediction = recorder.latest
                    inference_ms.append(prediction.speed["inference"])
                    boxes = [{"class_id": int(b.cls.item()), "confidence": round(float(b.conf.item()), 4),
                              "xyxy": [round(float(v), 2) for v in b.xyxy[0].tolist()]} for b in prediction.boxes]
                    timestamp = index / fps
                    if index < start_frame:
                        continue
                    display = render(frame, boxes, app.PARKING_SPACES[args.lot], states,
                                     title, timestamp, args.frame_skip, model_label)
                    writer.send(display.tobytes())
                    if exported == 0:
                        cv2.imwrite(str(output.with_suffix(".jpg")), display,
                                    [cv2.IMWRITE_JPEG_QUALITY, 92])
                    observations.append({"source_frame": index, "source_seconds": round(timestamp, 4),
                        "detections": boxes,
                        "states": {k: {"status": v["status"], "confidence": round(float(v["confidence"]), 4)}
                                   for k, v in states.items()}})
                    exported += 1
            finally:
                connection.close()
            metadata = {"model_file": args.model.name, "model_sha256": sha256(args.model),
                "model_names": trained_model.names, "model_parameters": parameter_count,
                "saved_checkpoint_date": trained_model.ckpt.get("date"),
                "saved_checkpoint_version": trained_model.ckpt.get("version"),
                "saved_validation_metrics": trained_model.ckpt.get("train_metrics"),
                "training_configuration": {k: trained_model.ckpt.get("train_args", {}).get(k)
                                           for k in ("model", "imgsz", "epochs", "patience", "batch", "seed")}}
        finally:
            os.chdir(original_cwd)
            cap.release()
            writer.close()

    metadata.update({"generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_file": args.video.name, "source_sha256": sha256(args.video),
        "source_size": source_size, "source_fps": fps, "source_total_frames": total,
        "lot_id": args.lot, "start_seconds": args.start, "duration_seconds": args.duration,
        "warmup_seconds": args.warmup, "frame_skip": args.frame_skip, "device": args.device,
        "cpu_threads": args.threads, "output_fps": fps / args.frame_skip,
        "output_frames": exported, "inference_calls_including_warmup": len(inference_ms),
        "median_model_inference_ms": round(statistics.median(inference_ms), 3),
        "runtime": {"python": platform.python_version(), "torch": torch.__version__,
                    "ultralytics": ultralytics.__version__, "opencv": cv2.__version__,
                    "system": platform.system(), "machine": platform.machine()},
        "observations": observations})
    write_report(output.with_suffix(".json"), metadata)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(output.with_suffix(".mp4")), "-vf",
        "fps=6,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=3",
        "-loop", "0", str(output.with_suffix(".gif"))], check=True)
    print(json.dumps({"output": str(output), "frames": exported,
                      "median_inference_ms": metadata["median_model_inference_ms"]}))


if __name__ == "__main__":
    main()
