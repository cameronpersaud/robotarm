#!/usr/bin/env python3
"""
Hand-tracking robot arm controller.

Camera -> MediaPipe hand landmarks -> servo angles -> Arduino over USB serial.

Hand controls (camera view is mirrored, like a selfie):
  move hand left/right       -> base rotates
  move hand up/down          -> shoulder
  move hand closer/farther   -> elbow
  tilt hand (roll)           -> wrist
  make a fist                -> gripper closes

Keys (in the preview window):
  q  quit           p  pause (arm holds still)        h  send arm home

Run:
  python3 hand_control.py --port /dev/ttyACM0     # real arm
  python3 hand_control.py --no-serial             # test tracking without the arm
  python3 hand_control.py --picam                 # Raspberry Pi Camera Module (picamera2)
"""

import argparse
import math
import os
import sys
import time
import urllib.request

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

# --------------------------------------------------------------------------
# CONFIG: tune these for your arm. Angles are servo degrees (0-180).
# --------------------------------------------------------------------------
# For each joint: (min_angle, max_angle, home_angle, invert)
# Start with narrow min/max ranges, then widen once you know the arm is safe.
SERVOS = {
    "base":     (20, 160, 90, False),
    "shoulder": (40, 140, 90, False),
    "elbow":    (40, 140, 90, False),
    "wrist":    (30, 150, 90, False),
    "gripper":  (30, 100, 30, False),   # min = open, max = closed
}
JOINT_ORDER = ["base", "shoulder", "elbow", "wrist", "gripper"]

# Input ranges: which part of the hand measurement maps onto the full servo range.
# Watch the numbers printed on the preview window and adjust these.
X_RANGE = (0.15, 0.85)        # palm x across the frame (0 = left, 1 = right)
Y_RANGE = (0.15, 0.85)        # palm y down the frame   (0 = top,  1 = bottom)
SIZE_RANGE = (0.08, 0.25)     # palm size (wrist->middle knuckle); bigger = closer
ROLL_RANGE = (-60.0, 60.0)    # hand tilt in degrees

# Fist detection uses hysteresis so the gripper doesn't flicker at the boundary.
# "openness" ~1.8+ for an open hand, ~1.0 or below for a fist.
FIST_CLOSE_BELOW = 1.15
FIST_OPEN_ABOVE = 1.40

# --- Floor boundary -------------------------------------------------------
# Measure your arm (cm) so the program knows where the floor is. Every command
# is checked before it is sent: if any part of the arm would go below the floor,
# the elbow (or, if that can't fix it, the shoulder) is raised just enough.
ARM_GEOMETRY = {
    "shoulder_height": 7.0,   # floor to the shoulder pivot
    "upper_arm": 15.0,        # shoulder pivot to elbow pivot
    "forearm": 13.0,          # elbow pivot to wrist
    "gripper": 6.0,           # wrist to fingertips
    "gripper_half_width": 2.5,  # center of gripper to outside of a finger (open)
}
FLOOR_MARGIN_CM = 1.5         # keep at least this much clearance above the floor
FLOOR_GUARD = True            # set False to disable (not recommended)

# How servo angles relate to the arm's pose. Check these on your arm:
SHOULDER_LEVEL_AT = 45        # shoulder servo angle where the upper arm is horizontal
SHOULDER_DIR = 1              # +1 if a bigger shoulder angle lifts the arm, else -1
ELBOW_STRAIGHT_AT = 150       # elbow servo angle where the forearm lines up with the upper arm
ELBOW_DIR = 1                 # +1 if a bigger elbow angle straightens/lifts the forearm, else -1
WRIST_LEVEL_AT = 90           # wrist servo angle where the gripper jaws are side by side

SMOOTHING = 0.35      # 0..1, lower = smoother but laggier
DEADBAND_DEG = 1.5    # don't resend unless some joint moved at least this much
SEND_HZ = 30          # max serial packets per second

MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/1/hand_landmarker.task")
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hand_landmarker.task")

# MediaPipe landmark indices
WRIST, THUMB_TIP = 0, 4
INDEX_MCP, MIDDLE_MCP, PINKY_MCP = 5, 9, 17
FINGER_TIPS = [8, 12, 16, 20]

HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
]


# --------------------------------------------------------------------------
# Hand measurements -> servo angles (pure functions, easy to test)
# --------------------------------------------------------------------------
def dist(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)


def remap(value, in_range, servo, flip=False):
    """Map a measurement in in_range onto a servo's (min, max), clamped."""
    lo, hi = in_range
    t = (value - lo) / (hi - lo)
    t = max(0.0, min(1.0, t))
    if flip:
        t = 1.0 - t
    mn, mx, _home, invert = SERVOS[servo]
    if invert:
        t = 1.0 - t
    return mn + t * (mx - mn)


def hand_features(lm):
    """Turn 21 landmarks into the few numbers we steer with."""
    palm = lm[MIDDLE_MCP]
    palm_size = dist(lm[WRIST], palm)
    tip_reach = sum(dist(lm[WRIST], lm[i]) for i in FINGER_TIPS) / len(FINGER_TIPS)
    openness = tip_reach / palm_size if palm_size > 1e-6 else 0.0
    # Roll: angle of the knuckle line (index -> pinky) relative to horizontal.
    dx = lm[PINKY_MCP].x - lm[INDEX_MCP].x
    dy = lm[PINKY_MCP].y - lm[INDEX_MCP].y
    roll = math.degrees(math.atan2(dy, dx))
    # Fold into -90..90 so left and right hands behave the same.
    if roll > 90:
        roll -= 180
    elif roll < -90:
        roll += 180
    return {"x": palm.x, "y": palm.y, "size": palm_size, "openness": openness, "roll": roll}


class FistDetector:
    """Hysteresis so the gripper doesn't chatter."""

    def __init__(self):
        self.closed = False

    def update(self, openness):
        if self.closed and openness > FIST_OPEN_ABOVE:
            self.closed = False
        elif not self.closed and openness < FIST_CLOSE_BELOW:
            self.closed = True
        return self.closed


def features_to_targets(f, fist_closed):
    """Hand features -> target angle for every joint."""
    g_min, g_max, _, g_inv = SERVOS["gripper"]
    grip = g_max if fist_closed else g_min
    if g_inv:
        grip = g_min if fist_closed else g_max
    return {
        "base": remap(f["x"], X_RANGE, "base"),
        "shoulder": remap(f["y"], Y_RANGE, "shoulder", flip=True),  # hand up = arm up
        "elbow": remap(f["size"], SIZE_RANGE, "elbow"),
        "wrist": remap(f["roll"], ROLL_RANGE, "wrist"),
        "gripper": grip,
    }


class Smoother:
    """Exponential moving average per joint. Gripper snaps (it's open/closed)."""

    def __init__(self, alpha=SMOOTHING):
        self.alpha = alpha
        self.state = {j: float(SERVOS[j][2]) for j in JOINT_ORDER}

    def update(self, targets):
        for j, t in targets.items():
            if j == "gripper":
                self.state[j] = t
            else:
                self.state[j] += self.alpha * (t - self.state[j])
        return dict(self.state)

    def reset_home(self):
        self.state = {j: float(SERVOS[j][2]) for j in JOINT_ORDER}


# --------------------------------------------------------------------------
# Arm kinematics + floor boundary
# --------------------------------------------------------------------------
def arm_kinematics(angles):
    """Joint positions in cm. Axes: x = right, y = forward, z = up (floor at z = 0)."""
    g = ARM_GEOMETRY
    th = math.radians(angles["base"] - 90)
    e1 = math.radians(SHOULDER_DIR * (angles["shoulder"] - SHOULDER_LEVEL_AT))
    e2 = e1 - math.radians(ELBOW_DIR * (ELBOW_STRAIGHT_AT - angles["elbow"]))
    roll = math.radians(angles["wrist"] - WRIST_LEVEL_AT)
    fx, fy = math.sin(th), math.cos(th)

    def along(p, elev, length):
        c = math.cos(elev) * length
        return (p[0] + c * fx, p[1] + c * fy, p[2] + math.sin(elev) * length)

    S = (0.0, 0.0, g["shoulder_height"])
    E = along(S, e1, g["upper_arm"])
    W = along(E, e2, g["forearm"])
    T = along(W, e2, g["gripper"])
    return {"th": th, "e1": e1, "e2": e2, "roll": roll, "S": S, "E": E, "W": W, "T": T}


def floor_clearance(angles):
    """Lowest height (cm) of any part of the arm above the floor."""
    k = arm_kinematics(angles)
    # When the wrist rolls, one jaw swings below the gripper's center line.
    spread = ARM_GEOMETRY["gripper_half_width"] * abs(math.sin(k["roll"]) * math.cos(k["e2"]))
    return min(k["E"][2], k["W"][2] - spread, k["T"][2] - spread)


def _is_safe(angles):
    return floor_clearance(angles) >= FLOOR_MARGIN_CM


def _nearest_safe(angles, joint, step=0.5):
    """Smallest change to one joint that clears the floor, or None."""
    mn, mx = SERVOS[joint][:2]
    start = angles[joint]
    trial = dict(angles)
    for i in range(1, int((mx - mn) / step) + 2):
        for v in (start + i * step, start - i * step):
            if mn <= v <= mx:
                trial[joint] = v
                if _is_safe(trial):
                    return abs(v - start), dict(trial)
    return None


def _elbow_height_ok(angles):
    return arm_kinematics(angles)["E"][2] >= FLOOR_MARGIN_CM


def enforce_floor(angles):
    """Returns (safe_angles, limited). limited is True when the pose had to be raised.

    Strategy: the arm stops at the floor and slides along it.
      1. Bend the elbow up just enough to clear the floor.
      2. If the elbow joint itself would be on the floor, lift the shoulder just
         enough first, then do step 1.
    Both steps make the smallest possible change, so the arm moves smoothly into
    and along the boundary instead of jumping between poses.
    """
    if not FLOOR_GUARD or _is_safe(angles):
        return dict(angles), False
    fix = _nearest_safe(angles, "elbow")
    if fix:
        return fix[1], True
    mn, mx = SERVOS["shoulder"][:2]
    start, trial, step = angles["shoulder"], dict(angles), 0.5
    for i in range(1, int((mx - mn) / step) + 2):
        for v in (start + i * step, start - i * step):
            if mn <= v <= mx:
                trial["shoulder"] = v
                if _elbow_height_ok(trial):
                    fix = _nearest_safe(trial, "elbow") if not _is_safe(trial) else (0, dict(trial))
                    if fix:
                        return fix[1], True
    # Nothing reachable clears the floor (check ARM_GEOMETRY): take the highest pose available.
    best = max(((s_, e_) for s_ in range(mn, mx + 1, 2)
                for e_ in range(SERVOS["elbow"][0], SERVOS["elbow"][1] + 1, 2)),
               key=lambda se: floor_clearance({**angles, "shoulder": se[0], "elbow": se[1]}))
    return {**angles, "shoulder": best[0], "elbow": best[1]}, True


def encode_packet(angles):
    """'<b,s,e,w,g>\\n' with integer degrees, the format the Arduino sketch parses."""
    return "<" + ",".join(str(int(round(angles[j]))) for j in JOINT_ORDER) + ">\n"



# --------------------------------------------------------------------------
# Serial link
# --------------------------------------------------------------------------
class ArmLink:
    """Sends angle packets to the Arduino. port=None means no arm attached."""

    def __init__(self, port, baud=115200, echo=False):
        self.ser = None
        self.echo = echo
        self.last_sent = None
        self.last_time = 0.0
        self.port = port
        if port is None:
            return
        import serial  # pyserial
        self.ser = serial.Serial(port, baud, timeout=0)
        print(f"[serial] opened {port}, waiting for Arduino reset...")
        time.sleep(2.0)  # opening the port resets most Arduinos
        self.ser.reset_input_buffer()

    @property
    def connected(self):
        return self.ser is not None

    def send(self, angles, force=False):
        now = time.time()
        if not force and now - self.last_time < 1.0 / SEND_HZ:
            return False
        if not force and self.last_sent is not None:
            moved = max(abs(angles[j] - self.last_sent[j]) for j in JOINT_ORDER)
            if moved < DEADBAND_DEG:
                return False
        pkt = encode_packet(angles)
        if self.ser:
            self.ser.write(pkt.encode("ascii"))
        elif self.echo:
            print(pkt.strip())
        self.last_sent = dict(angles)
        self.last_time = now
        return True

    def read_debug(self):
        if self.ser and self.ser.in_waiting:
            try:
                for line in self.ser.read(self.ser.in_waiting).decode(errors="ignore").splitlines():
                    if line.strip():
                        print("[arduino]", line.strip())
            except Exception:
                pass

    def close(self):
        if self.ser:
            self.ser.close()
            self.ser = None


def list_serial_ports():
    """[(device, description), ...] for the port picker."""
    try:
        from serial.tools import list_ports
        return [(p.device, p.description or p.device) for p in list_ports.comports()]
    except Exception:
        return []


# --------------------------------------------------------------------------
# Camera
# --------------------------------------------------------------------------
class Camera:
    def __init__(self, index, width, height, use_picam):
        self.picam = None
        self.cap = None
        if use_picam:
            from picamera2 import Picamera2
            self.picam = Picamera2()
            cfg = self.picam.create_preview_configuration(
                main={"size": (width, height), "format": "RGB888"})
            self.picam.configure(cfg)
            self.picam.start()
        else:
            self.cap = cv2.VideoCapture(index)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            if not self.cap.isOpened():
                raise RuntimeError(f"Could not open camera {index}. "
                                   "Try another camera number, or the Pi camera option.")

    def read(self):
        """Returns a BGR frame or None."""
        if self.picam:
            return self.picam.capture_array()  # "RGB888" in picamera2 is BGR order in memory
        ok, frame = self.cap.read()
        return frame if ok else None

    def release(self):
        if self.picam:
            self.picam.stop()
        elif self.cap:
            self.cap.release()


def _ssl_context():
    """Use certifi's certificates when available. python.org installs on macOS
    often ship without system certificates, which breaks HTTPS downloads."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def ensure_model():
    if os.path.exists(MODEL_PATH):
        return
    print(f"Downloading hand model to {MODEL_PATH} ...")
    tmp = MODEL_PATH + ".part"
    try:
        with urllib.request.urlopen(MODEL_URL, context=_ssl_context(), timeout=60) as r, \
                open(tmp, "wb") as f:
            while chunk := r.read(1 << 16):
                f.write(chunk)
        os.replace(tmp, MODEL_PATH)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        hint = ""
        if "CERTIFICATE_VERIFY_FAILED" in str(e):
            hint = ("Fix: run 'pip install certifi' and try again, or on macOS run "
                    "'Install Certificates.command' from your Python folder in Applications. ")
        raise RuntimeError(
            f"Couldn't download the hand model ({e}). {hint}"
            f"Or download it manually from {MODEL_URL} and save it as {MODEL_PATH}") from None


def create_landmarker():
    ensure_model()
    options = vision.HandLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=MODEL_PATH),
        running_mode=vision.RunningMode.VIDEO,
        num_hands=1,
        min_hand_detection_confidence=0.6,
        min_hand_presence_confidence=0.6,
        min_tracking_confidence=0.5,
    )
    return vision.HandLandmarker.create_from_options(options)


# --------------------------------------------------------------------------
# Tracker: camera + hand model + mapping + serial, one frame at a time.
# Used by both the command line (below) and the GUI (arm_gui.py).
# --------------------------------------------------------------------------
class ArmTracker:
    def __init__(self, port=None, baud=115200, camera=0, picam=False,
                 width=320, height=240, echo=False):
        self.landmarker = create_landmarker()
        self.cam = Camera(camera, width, height, picam)
        try:
            self.link = ArmLink(port, baud, echo=echo)
        except Exception:
            self.cam.release()
            self.landmarker.close()
            raise
        self.smoother = Smoother()
        self.fist = FistDetector()
        self.paused = False
        self.angles, self.floor_limited = enforce_floor(self.smoother.state)
        self.link.send(self.angles, force=True)  # start at home
        self._t0 = time.monotonic()
        self._last_ts = -1
        self.fps = 0.0
        self._fps_count, self._fps_t = 0, time.time()

    def step(self):
        """Grab one frame, track, move the arm. Returns a dict, or None if no frame."""
        frame = self.cam.read()
        if frame is None:
            return None
        frame = cv2.flip(frame, 1)  # mirror so it feels natural
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        ts = int((time.monotonic() - self._t0) * 1000)
        if ts <= self._last_ts:          # VIDEO mode needs strictly increasing timestamps
            ts = self._last_ts + 1
        self._last_ts = ts
        result = self.landmarker.detect_for_video(mp_image, ts)

        lm, feats = None, None
        if result.hand_landmarks:
            lm = result.hand_landmarks[0]
            feats = hand_features(lm)
            self.fist.update(feats["openness"])
            if not self.paused:
                smoothed = self.smoother.update(features_to_targets(feats, self.fist.closed))
                # Floor boundary is applied last, so nothing unsafe ever reaches the arm.
                self.angles, self.floor_limited = enforce_floor(smoothed)
                self.link.send(self.angles)
        # No hand: send nothing, so the arm holds its last position.
        self.link.read_debug()

        self._fps_count += 1
        now = time.time()
        if now - self._fps_t >= 1.0:
            self.fps = self._fps_count / (now - self._fps_t)
            self._fps_count, self._fps_t = 0, now

        return {"frame": frame, "landmarks": lm, "features": feats, "fist": self.fist.closed,
                "angles": dict(self.angles), "floor_limited": self.floor_limited,
                "paused": self.paused, "fps": self.fps}

    def toggle_pause(self):
        self.paused = not self.paused
        return self.paused

    def home(self):
        self.smoother.reset_home()
        self.angles, self.floor_limited = enforce_floor(self.smoother.state)
        self.link.send(self.angles, force=True)

    def close(self):
        self.cam.release()
        self.link.close()
        self.landmarker.close()


def run_headless(tracker, should_stop=lambda: False):
    """Console-only monitoring loop (used for the Raspberry Pi). Ctrl+C to stop."""
    print("Running headless. Press Ctrl+C to stop.")
    last_print = 0.0
    try:
        while not should_stop():
            state = tracker.step()
            if state is None:
                continue
            now = time.time()
            if now - last_print >= 1.0:
                last_print = now
                hand = "FIST" if state["fist"] else "open"
                if state["features"] is None:
                    hand = "no hand"
                print(f"FPS {state['fps']:4.1f} | {hand:7s} | "
                      + "  ".join(f"{j}:{int(state['angles'][j])}" for j in JOINT_ORDER)
                      + ("  | FLOOR LIMIT" if state["floor_limited"] else ""))
    except KeyboardInterrupt:
        pass
    finally:
        tracker.close()
        print("Stopped.")


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------
def draw_skeleton(frame, lm, fist=False):
    """Stick model of the tracked hand, drawn onto the frame in place."""
    if lm is None:
        return
    h, w = frame.shape[:2]
    thick = max(2, w // 220)
    pts = [(int(p.x * w), int(p.y * h)) for p in lm]
    bone = (80, 200, 255) if fist else (168, 182, 63)   # BGR: amber when gripping, teal when open
    for a, b in HAND_CONNECTIONS:
        cv2.line(frame, pts[a], pts[b], (20, 20, 20), thick + 3, cv2.LINE_AA)
        cv2.line(frame, pts[a], pts[b], bone, thick, cv2.LINE_AA)
    for i, p in enumerate(pts):
        r = thick + 3 if i in (WRIST, MIDDLE_MCP) else thick + 1
        cv2.circle(frame, p, r + 1, (20, 20, 20), -1, cv2.LINE_AA)
        cv2.circle(frame, p, r, (255, 255, 255), -1, cv2.LINE_AA)
    # Palm center marker: this is the point that steers base and shoulder.
    cv2.circle(frame, pts[MIDDLE_MCP], thick * 5, bone, thick, cv2.LINE_AA)


def draw_overlay(frame, state):
    draw_skeleton(frame, state["landmarks"], state["fist"])
    feats, angles = state["features"], state["angles"]
    lines = [f"FPS {state['fps']:4.1f}" + ("   PAUSED" if state["paused"] else "")]
    if feats:
        lines.append(f"x {feats['x']:.2f}  y {feats['y']:.2f}  size {feats['size']:.3f}")
        lines.append(f"roll {feats['roll']:+.0f}  open {feats['openness']:.2f}  "
                     f"{'FIST' if state['fist'] else 'open'}")
    else:
        lines.append("no hand - arm holding")
    lines.append("  ".join(f"{j[:2]}:{int(angles[j])}" for j in JOINT_ORDER))
    if state.get("floor_limited"):
        lines.append("FLOOR LIMIT - arm held above the floor")
    for i, text in enumerate(lines):
        y = 22 + i * 22
        cv2.putText(frame, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
        cv2.putText(frame, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)


# --------------------------------------------------------------------------
# Command line entry point (the GUI lives in arm_gui.py)
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Hand-tracking robot arm controller")
    ap.add_argument("--port", default="/dev/ttyACM0", help="Arduino serial port (Windows: COM3 etc.)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--no-serial", action="store_true", help="run without the Arduino")
    ap.add_argument("--camera", type=int, default=0, help="webcam index")
    ap.add_argument("--picam", action="store_true", help="use a Pi Camera Module via picamera2")
    ap.add_argument("--width", type=int, default=320, help="320x240 is a good speed on a Pi")
    ap.add_argument("--height", type=int, default=240)
    ap.add_argument("--headless", action="store_true", help="no preview window (e.g. over SSH)")
    args = ap.parse_args()

    try:
        tracker = ArmTracker(None if args.no_serial else args.port, args.baud, args.camera,
                             args.picam, args.width, args.height, echo=args.no_serial)
    except Exception as e:
        sys.exit(f"Startup failed: {e}")

    if args.headless:
        run_headless(tracker)
        return

    try:
        while True:
            state = tracker.step()
            if state is None:
                continue
            frame = state["frame"]
            draw_overlay(frame, state)
            cv2.imshow("Hand Arm Control", frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("p"):
                tracker.toggle_pause()
            if key == ord("h"):
                tracker.home()
    except KeyboardInterrupt:
        pass
    finally:
        tracker.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
