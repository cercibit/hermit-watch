"""HermitWatch motion detection, motion-triggered clips, time-lapse and storage.

Layout under REC_DIR (default ~/hermitwatch):
  clips/YYYY-MM-DD/HHMMSS.mp4   motion clip (H.264, includes pre-roll)
  clips/YYYY-MM-DD/HHMMSS.jpg   thumbnail taken when motion started
  clips/YYYY-MM-DD/HHMMSS.json  {"start", "end", "duration", "peak"} written when the clip closes
  timelapse/YYYY-MM-DD/HHMMSS.jpg   time-lapse stills
  timelapse/YYYY-MM-DD.mp4          time-lapse video built on request
"""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np
from picamera2.outputs import PyavOutput

log = logging.getLogger("recorder")

REC_DIR = os.path.realpath(os.path.expanduser(os.environ.get("REC_DIR", "~/hermitwatch")))
CLIPS = os.path.join(REC_DIR, "clips")
TIMELAPSE = os.path.join(REC_DIR, "timelapse")
MIN_FREE_BYTES = 1 << 30  # stop writing when the SD card has < 1 GB free
LORES = (320, 240)        # motion-detection stream size
BLOCK = 4                 # average 4x4 blocks -> 80x60 grid, suppresses sensor noise
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STEM_RE = re.compile(r"^clips/\d{4}-\d{2}-\d{2}/\d{6}(-\d+)?$")


def disk_ok():
    os.makedirs(REC_DIR, exist_ok=True)
    return shutil.disk_usage(REC_DIR).free > MIN_FREE_BYTES


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def unique_stem(directory, now):
    stem = os.path.join(directory, now.strftime("%H%M%S"))
    n, candidate = 1, stem
    while os.path.exists(candidate + ".mp4") or os.path.exists(candidate + ".jpg"):
        n += 1
        candidate = f"{stem}-{n}"
    return candidate


class Recorder(threading.Thread):
    """Watches the lores stream for motion and drives clip/time-lapse recording.

    get_settings() -> dict snapshot of current settings
    grab_lores()   -> Y-plane numpy array (H, W) of the lores stream (may raise while reconfiguring)
    latest_jpeg()  -> most recent JPEG bytes from the live MJPEG stream (or None)
    get_circular() -> the CircularOutput2 attached to the H.264 encoder
    preroll_s      -> the CircularOutput2 buffer length. Frames reach the file only as they
                      age out of that buffer, so a clip is closed preroll_s after activity ends.
    """

    def __init__(self, get_settings, grab_lores, latest_jpeg, get_circular, camera_lock, preroll_s):
        super().__init__(daemon=True, name="recorder")
        self.get_settings = get_settings
        self.grab_lores = grab_lores
        self.latest_jpeg = latest_jpeg
        self.get_circular = get_circular
        self.camera_lock = camera_lock
        self.preroll_s = preroll_s
        self.lock = threading.Lock()
        self.force_until = 0.0    # "Record now" keeps recording until this monotonic time
        self.last_hot = 0.0       # last time motion/forced recording was active
        self.score = 0.0          # % of the frame that changed in the last check
        self.last_motion = 0.0    # monotonic time of last confirmed motion
        self.last_motion_wall = None
        self.lighting_change = False
        self.clip = None          # dict for the clip being written
        self.last_still = 0.0
        self.building = set()     # dates whose time-lapse video is being built
        self.history = deque()
        self.hits = 0

    # ---- motion -----------------------------------------------------------

    def _small(self):
        y = self.grab_lores()[: LORES[1], : LORES[0]].astype(np.float32)
        h, w = y.shape[0] // BLOCK, y.shape[1] // BLOCK
        return y[: h * BLOCK, : w * BLOCK].reshape(h, BLOCK, w, BLOCK).mean(axis=(1, 3))

    def _detect(self, now, small, s):
        """Compare against a frame ~motion_gap seconds old so slow crabs still register."""
        self.history.append((now, small))
        while len(self.history) > 1 and now - self.history[1][0] >= s["motion_gap"]:
            self.history.popleft()
        ref_t, ref = self.history[0]
        if now - ref_t < s["motion_gap"] * 0.8:
            return False  # not enough history yet
        diff = np.abs(small - ref)
        changed = float((diff > s["motion_threshold"]).mean() * 100)
        self.score = changed
        self.max_diff = float(diff.max())
        self.level = float(small.mean())
        # A huge change is a lamp switching or auto-exposure jumping, not a crab: rebase.
        if changed > 40:
            self.lighting_change = True
            self.history.clear()
            self.hits = 0
            return False
        self.lighting_change = False
        self.hits = self.hits + 1 if changed >= s["motion_area"] else 0
        return self.hits >= 2  # two checks in a row filters single-frame noise

    def run(self):
        while True:
            time.sleep(0.25)
            s = self.get_settings()
            try:
                with self.camera_lock:
                    small = self._small()
                    now = time.monotonic()
                    if self._detect(now, small, s):
                        self.last_motion = now
                        self.last_motion_wall = time.time()
                    self._tick(now, s)
            except Exception:
                log.exception("recorder loop error")
                time.sleep(2)

    # ---- recording --------------------------------------------------------

    def record_now(self, seconds):
        self.force_until = time.monotonic() + max(1, min(600, float(seconds)))

    def _tick(self, now, s):
        forced = now < self.force_until
        motion = s["rec_clips"] and self.last_motion and now - self.last_motion < s["post_roll"]
        if forced or motion:
            self.last_hot = now
        clip = self.clip
        if clip:
            # Keep the output open until the pre-roll buffer has drained the hot period into the file.
            drained = now - self.last_hot > self.preroll_s
            too_long = now - clip["t0"] > s["max_clip"]
            if drained or too_long or not disk_ok():
                self.close_clip()
                clip = None
            else:
                clip["peak"] = max(clip["peak"], self.score)
        if clip is None and (forced or motion) and disk_ok():
            self.open_clip(manual=forced and not motion)

        if s["tl_enabled"] and now - self.last_still >= s["tl_interval"]:
            moving_recently = self.last_motion and now - self.last_motion < max(s["post_roll"], s["tl_interval"])
            if (moving_recently or not s["tl_motion_only"]) and disk_ok():
                self.save_still()
                self.last_still = now

    def open_clip(self, manual=False):
        now = datetime.now()
        day = os.path.join(CLIPS, now.strftime("%Y-%m-%d"))
        os.makedirs(day, exist_ok=True)
        stem = unique_stem(day, now)
        jpeg = self.latest_jpeg()
        if jpeg:
            with open(stem + ".jpg", "wb") as f:
                f.write(jpeg)
        self.get_circular().open_output(PyavOutput(stem + ".mp4"))
        self.clip = {"stem": stem, "t0": time.monotonic(), "opened": time.time(), "peak": self.score, "manual": manual}
        log.info("clip started %s", os.path.relpath(stem, REC_DIR))

    def close_clip(self, flushed=False):
        """Finish the current clip. flushed=True when stopping the encoder already
        flushed the whole buffer into the file (camera reconfigure/shutdown)."""
        with self.lock:
            clip, self.clip = self.clip, None
        if not clip:
            return
        if not flushed:
            try:
                self.get_circular().close_output()
            except Exception:
                log.exception("closing clip output")
        # The file covers [opened - preroll, closed - preroll] (or up to now if flushed).
        start = clip["opened"] - self.preroll_s
        end = time.time() - (0 if flushed else self.preroll_s)
        write_json(clip["stem"] + ".json", {
            "start": start, "end": end, "duration": round(end - start, 1),
            "peak": round(clip["peak"], 2), "manual": clip["manual"],
        })
        log.info("clip saved %s (%.0fs)", os.path.relpath(clip["stem"], REC_DIR), end - start)

    def save_still(self):
        jpeg = self.latest_jpeg()
        if not jpeg:
            return
        now = datetime.now()
        day = os.path.join(TIMELAPSE, now.strftime("%Y-%m-%d"))
        os.makedirs(day, exist_ok=True)
        with open(unique_stem(day, now) + ".jpg", "wb") as f:
            f.write(jpeg)

    # ---- status / library ---------------------------------------------------

    def status(self):
        now = time.monotonic()
        return {
            "score": round(self.score, 2),
            "moving": bool(self.last_motion and now - self.last_motion < 1.5),
            "recording": self.clip is not None,
            "forced": time.monotonic() < self.force_until,
            "lighting_change": self.lighting_change,
            "last_motion": self.last_motion_wall,
            "max_diff": round(getattr(self, "max_diff", -1), 1),
            "level": round(getattr(self, "level", -1), 1),
            "history": len(self.history),
        }

    def library(self):
        clips, timelapse = [], []
        active = self.clip["stem"] if self.clip else None
        if os.path.isdir(CLIPS):
            for day in sorted(os.listdir(CLIPS), reverse=True):
                ddir = os.path.join(CLIPS, day)
                if not DATE_RE.match(day) or not os.path.isdir(ddir):
                    continue
                for name in sorted((n for n in os.listdir(ddir) if n.endswith(".mp4")), reverse=True):
                    stem = os.path.join(ddir, name[:-4])
                    midnight = datetime.strptime(day, "%Y-%m-%d").timestamp()
                    meta = {}
                    if os.path.exists(stem + ".json"):
                        with open(stem + ".json") as f:
                            meta = json.load(f)
                    else:  # still recording (or interrupted): estimate from the file name
                        start = midnight + int(name[0:2]) * 3600 + int(name[2:4]) * 60 + int(name[4:6]) - self.preroll_s
                        end = time.time() if stem == active else os.path.getmtime(stem + ".mp4")
                        meta = {"start": start, "end": end, "duration": round(max(0, end - start), 1)}
                    meta["tod"] = round(max(0.0, meta["start"] - midnight), 1)  # seconds since local midnight
                    clips.append({
                        "id": os.path.relpath(stem, REC_DIR),
                        "date": day,
                        "time": f"{name[0:2]}:{name[2:4]}:{name[4:6]}",
                        "size": os.path.getsize(stem + ".mp4"),
                        "thumb": os.path.exists(stem + ".jpg"),
                        "recording": stem == active,
                        **meta,
                    })
        if os.path.isdir(TIMELAPSE):
            for day in sorted(os.listdir(TIMELAPSE), reverse=True):
                ddir = os.path.join(TIMELAPSE, day)
                if not DATE_RE.match(day) or not os.path.isdir(ddir):
                    continue
                stills = sorted(n for n in os.listdir(ddir) if n.endswith(".jpg"))
                video = os.path.join(TIMELAPSE, day + ".mp4")
                timelapse.append({
                    "date": day,
                    "count": len(stills),
                    "first": stills[0][:6] if stills else None,
                    "last": stills[-1][:6] if stills else None,
                    "latest": f"timelapse/{day}/{stills[-1]}" if stills else None,
                    "video": os.path.exists(video) and day not in self.building,
                    "video_size": os.path.getsize(video) if os.path.exists(video) else 0,
                    "building": day in self.building,
                })
        usage = shutil.disk_usage(REC_DIR) if os.path.isdir(REC_DIR) else None
        return {
            "clips": clips,
            "timelapse": timelapse,
            "disk": {"used": dir_size(REC_DIR), "free": usage.free if usage else 0},
        }

    def delete_clip(self, clip_id):
        if not STEM_RE.match(clip_id):
            raise ValueError("bad clip id")
        stem = os.path.join(REC_DIR, clip_id)
        if self.clip and self.clip["stem"] == stem:
            raise ValueError("clip is still recording")
        for ext in (".mp4", ".jpg", ".json"):
            if os.path.exists(stem + ext):
                os.remove(stem + ext)

    def delete_timelapse(self, day, video_only=False):
        if not DATE_RE.match(day):
            raise ValueError("bad date")
        if day in self.building:
            raise ValueError("video is being built")
        video = os.path.join(TIMELAPSE, day + ".mp4")
        if os.path.exists(video):
            os.remove(video)
        if not video_only:
            shutil.rmtree(os.path.join(TIMELAPSE, day), ignore_errors=True)

    def build_timelapse(self, day, fps=15):
        """Encode a day's stills to MP4 in the background with ffmpeg."""
        if not DATE_RE.match(day):
            raise ValueError("bad date")
        ddir = os.path.join(TIMELAPSE, day)
        if not os.path.isdir(ddir) or not any(n.endswith(".jpg") for n in os.listdir(ddir)):
            raise ValueError("no stills for that day")
        if day in self.building:
            return
        self.building.add(day)
        fps = max(1, min(60, int(fps)))

        def run():
            out = os.path.join(TIMELAPSE, day + ".mp4")
            tmp = out + ".part.mp4"
            cmd = ["nice", "-n", "10", "ffmpeg", "-y", "-loglevel", "error",
                   "-framerate", str(fps), "-pattern_type", "glob", "-i", os.path.join(ddir, "*.jpg"),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", tmp]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode == 0:
                    os.replace(tmp, out)
                    log.info("time-lapse built %s", os.path.relpath(out, REC_DIR))
                else:
                    log.error("ffmpeg failed: %s", r.stderr[-500:])
                    if os.path.exists(tmp):
                        os.remove(tmp)
            finally:
                self.building.discard(day)

        threading.Thread(target=run, daemon=True).start()


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for n in files:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total


class Janitor(threading.Thread):
    """Deletes the oldest recordings when REC_DIR grows past storage_gb."""

    def __init__(self, get_settings, recorder):
        super().__init__(daemon=True, name="janitor")
        self.get_settings = get_settings
        self.recorder = recorder

    def run(self):
        while True:
            try:
                self.sweep()
            except Exception:
                log.exception("janitor")
            time.sleep(300)

    def sweep(self):
        limit = self.get_settings()["storage_gb"] * (1 << 30)
        files = []
        for root, _, names in os.walk(REC_DIR):
            for n in names:
                p = os.path.join(root, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
        total = sum(f[1] for f in files)
        if total <= limit:
            return
        active = self.recorder.clip["stem"] if self.recorder.clip else None
        for _, size, p in sorted(files):
            if total <= limit:
                break
            if active and p.startswith(active + "."):
                continue
            os.remove(p)
            total -= size
        for root, dirs, names in os.walk(REC_DIR, topdown=False):
            if root != REC_DIR and not dirs and not names:
                os.rmdir(root)
        log.info("janitor trimmed recordings to %.1f GB", total / (1 << 30))
