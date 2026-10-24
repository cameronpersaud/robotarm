# Hand-Tracking Robot Arm

Camera → MediaPipe hand tracking → servo angles → Arduino over USB.

| Your hand | Arm joint |
|---|---|
| Left / right | Base |
| Up / down | Shoulder |
| Closer / farther from camera | Elbow |
| Tilt (roll) | Wrist |
| Make a fist | Gripper closes |

If the hand leaves the frame, the arm holds its last position.

## Setup

**Arduino:** open `arm_receiver/arm_receiver.ino`, set `SERVO_PINS`, `HOME`, `MIN_ANGLE`, `MAX_ANGLE` for your arm, upload.
Power the servos from a separate 5–6V supply and connect its ground to Arduino GND.

**Pi / laptop:**
```bash
pip install mediapipe opencv-python pyserial
# Pi Camera Module only: sudo apt install python3-picamera2
```
The hand model (`hand_landmarker.task`, ~8MB) downloads automatically on first run. Do that run before the
hackathon in case venue Wi-Fi is bad, and keep the file next to the script.

## Run the app (laptop or Pi with a screen)

```bash
pip install pillow        # extra dependency for the app
python3 arm_gui.py
```
Welcome → Instructions → choose **PC / Laptop** (live video + stick model + directions)
or **Raspberry Pi** (window closes, tracking continues headless in the terminal).
On a Pi with no screen at all, `arm_gui.py` goes straight to headless mode;
pass `--port /dev/ttyACM0` (and `--picam` for a Pi Camera Module).

If Tkinter is missing on Linux: `sudo apt install python3-tk`.

## Run from the command line (no app)

```bash
python3 hand_control.py --no-serial          # 1. check tracking, no arm attached
python3 hand_control.py --port /dev/ttyACM0  # 2. drive the arm (Windows: --port COM3)
```
Add `--picam` for a Pi Camera Module, `--headless` over SSH.
Keys in the preview window: **q** quit, **p** pause (arm freezes), **h** send home.

## Tuning (in order)

1. **Servo limits first.** Set safe `MIN/MAX` in the Arduino sketch so the arm can never hit itself or the table.
   The Python `SERVOS` table should sit inside those limits.
   **Floor boundary:** measure your arm and enter the lengths (cm) in `ARM_GEOMETRY` in
   `hand_control.py`. Then check the pose settings with the arm powered and moving slowly:
   - `SHOULDER_LEVEL_AT`: the shoulder servo angle where the upper arm is horizontal
   - `ELBOW_STRAIGHT_AT`: the elbow servo angle where the forearm lines up with the upper arm
   - `SHOULDER_DIR` / `ELBOW_DIR`: set to -1 if a bigger angle moves that joint *down*

   The 3D model uses the same numbers, so if the model's pose matches the real arm, the
   floor limit is accurate. When the limit is active, the app shows "Floor limit" and the
   arm hovers `FLOOR_MARGIN_CM` above the floor instead of following your hand down.
2. **Input ranges.** The preview shows live `x`, `y`, `size`, `roll`, `open` values. Move your hand to the
   comfortable edges of your reach and copy those numbers into `X_RANGE`, `Y_RANGE`, `SIZE_RANGE`, `ROLL_RANGE`.
3. **Direction.** If a joint moves the wrong way, flip `invert` for it in `SERVOS`.
4. **Fist threshold.** Note `open` for your open hand and fist; set `FIST_CLOSE_BELOW` / `FIST_OPEN_ABOVE`
   between them (keep a gap so it doesn't flicker).
5. **Feel.** Jittery → lower `SMOOTHING` or raise `DEADBAND_DEG`. Laggy → raise `SMOOTHING`.
   Arm too slow/fast physically → `MAX_STEP_DEG` in the sketch.

## If it's slow on the Pi

Keep 320×240 (the default). If you're still under ~10 FPS, run the script on a laptop
with the Arduino plugged into it instead. Nothing else changes.
