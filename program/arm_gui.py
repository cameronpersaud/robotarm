#!/usr/bin/env python3
"""
Hand-Controlled Robot Arm: desktop app.

Screens: Welcome -> Instructions -> Setup (PC or Raspberry Pi) -> Live control.
  PC:            live camera feed with the tracked hand's stick model on the right,
                 directions and joint readouts on the left.
  Raspberry Pi:  the window closes and tracking continues headless in the terminal.

Run:   python3 arm_gui.py
Needs: pip install mediapipe opencv-python pyserial pillow   (Tkinter ships with Python)
"""

import argparse
import math
import os
import platform
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

import cv2
from PIL import Image, ImageTk

import hand_control as hc

# --------------------------------------------------------------------------
# Look and feel
# --------------------------------------------------------------------------
BG = "#0f141a"
PANEL = "#18202a"
PANEL_HI = "#212b37"
LINE = "#2c3846"
TEXT = "#e9eef3"
MUTED = "#8fa0b2"
ACCENT = "#3fb6a8"       # teal: open hand / primary
ACCENT_DK = "#2e8f84"
WARM = "#ffc850"         # amber: gripping
DANGER = "#e5675c"

PREVIEW_ONLY = "No arm (preview only)"
PC_RES = (640, 480)
PI_RES = (320, 240)


def pick_font(*candidates):
    available = set(tkfont.families())
    for name in candidates:
        if name in available:
            return name
    return "TkDefaultFont"


class Fonts:
    def __init__(self):
        fam = pick_font("Segoe UI", "Helvetica Neue", "SF Pro Text", "Inter",
                        "Ubuntu", "DejaVu Sans", "Helvetica", "Arial")
        mono = pick_font("Cascadia Mono", "Menlo", "Consolas", "DejaVu Sans Mono", "Courier")
        self.hero = (fam, 30, "bold")
        self.h1 = (fam, 22, "bold")
        self.h2 = (fam, 14, "bold")
        self.body = (fam, 12)
        self.small = (fam, 10)
        self.label = (fam, 10, "bold")
        self.label_lg = (fam, 12, "bold")
        self.button = (fam, 12, "bold")
        self.mono = (mono, 11)


def rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class FlatButton(tk.Canvas):
    """Rounded button drawn on a canvas, so it looks the same on Windows, macOS and Linux."""

    def __init__(self, parent, text, command, fonts, kind="primary", width=150, height=44):
        super().__init__(parent, width=width, height=height, bg=parent["bg"],
                         highlightthickness=0, bd=0, cursor="hand2")
        self.command, self.fonts, self.kind = command, fonts, kind
        self.text, self.enabled, self.hover = text, True, False
        self.w, self.h = width, height
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._draw()

    def _colors(self):
        if not self.enabled:
            return PANEL, LINE, MUTED
        if self.kind == "primary":
            return (ACCENT if self.hover else ACCENT_DK), ACCENT, "#06110f"
        if self.kind == "danger":
            return (DANGER if self.hover else PANEL_HI), DANGER, TEXT
        return (PANEL_HI if self.hover else PANEL), LINE, TEXT

    def _draw(self):
        self.delete("all")
        fill, outline, fg = self._colors()
        rounded_rect(self, 2, 2, self.w - 2, self.h - 2, 12, fill=fill, outline=outline, width=1.5)
        self.create_text(self.w / 2, self.h / 2, text=self.text, fill=fg, font=self.fonts.button)

    def _set_hover(self, on):
        self.hover = on and self.enabled
        self._draw()

    def _click(self, _e):
        if self.enabled and self.command:
            self.command()

    def configure_button(self, text=None, enabled=None, kind=None):
        if text is not None:
            self.text = text
        if enabled is not None:
            self.enabled = enabled
            self.config(cursor="hand2" if enabled else "arrow")
        if kind is not None:
            self.kind = kind
        self._draw()


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
class Page(tk.Frame):
    def __init__(self, app):
        super().__init__(app.root, bg=BG)
        self.app, self.f = app, app.fonts

    def on_show(self):
        pass

    def on_hide(self):
        pass


class WelcomePage(Page):
    def __init__(self, app):
        super().__init__(app)
        wrap = tk.Frame(self, bg=BG)
        wrap.place(relx=0.5, rely=0.42, anchor="center")

        tk.Label(wrap, text="HACKATHON PROJECT", font=self.f.label, fg=ACCENT, bg=BG).pack()
        tk.Label(wrap, text="Hand-Controlled Robot Arm", font=self.f.hero,
                 fg=TEXT, bg=BG).pack(pady=(6, 10))
        tk.Label(wrap, text="Welcome! Move your hand in front of a camera and the robot arm "
                            "moves with you. Make a fist and the gripper grabs.",
                 font=self.f.body, fg=MUTED, bg=BG, wraplength=620, justify="center").pack()

        diagram = tk.Canvas(wrap, width=840, height=130, bg=BG, highlightthickness=0)
        diagram.pack(pady=26)
        steps = [("Camera", "sees your hand"), ("Hand tracking", "21 points, live"),
                 ("Joint angles", "hand → arm"), ("Arduino", "drives servos"),
                 ("Robot arm", "follows you")]
        bw, gap = 140, 30
        x = (840 - (len(steps) * bw + (len(steps) - 1) * gap)) / 2
        for i, (title, sub) in enumerate(steps):
            rounded_rect(diagram, x, 20, x + bw, 110, 14, fill=PANEL,
                         outline=ACCENT if i in (0, len(steps) - 1) else LINE, width=1.5)
            diagram.create_text(x + bw / 2, 55, text=title, fill=TEXT, font=self.f.label_lg)
            diagram.create_text(x + bw / 2, 82, text=sub, fill=MUTED, font=self.f.small)
            if i < len(steps) - 1:
                ax = x + bw + 6
                diagram.create_line(ax, 65, ax + gap - 12, 65, fill=ACCENT, width=2,
                                    arrow="last", arrowshape=(9, 10, 4))
            x += bw + gap

        tk.Label(wrap, text="Why it matters: low-cost, touch-free control for assistive "
                            "robotics, remote handling and rehab.",
                 font=self.f.small, fg=MUTED, bg=BG).pack()


class InstructionsPage(Page):
    def __init__(self, app):
        super().__init__(app)
        wrap = tk.Frame(self, bg=BG)
        wrap.place(relx=0.5, rely=0.43, anchor="center")
        tk.Label(wrap, text="How to use it", font=self.f.h1, fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(wrap, text="Read this once, then you're ready to go.", font=self.f.body,
                 fg=MUTED, bg=BG).pack(anchor="w", pady=(2, 18))

        cols = tk.Frame(wrap, bg=BG)
        cols.pack()

        left = tk.Frame(cols, bg=PANEL, padx=22, pady=18, highlightbackground=LINE,
                        highlightthickness=1)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        tk.Label(left, text="Getting started", font=self.f.h2, fg=TEXT, bg=PANEL).pack(anchor="w")
        steps = [
            "Plug the Arduino into this computer with USB (or skip it to just preview).",
            "Sit about an arm's length from the camera, in good light.",
            "Hold up ONE hand, palm facing the camera.",
            "Move slowly at first. If your hand leaves the view, the arm holds still.",
            "Use Pause any time to freeze the arm, and Home to reset it.",
        ]
        for i, s in enumerate(steps, 1):
            row = tk.Frame(left, bg=PANEL)
            row.pack(anchor="w", pady=5, fill="x")
            tk.Label(row, text=str(i), font=self.f.label, fg="#06110f", bg=ACCENT,
                     width=2).pack(side="left", anchor="n", pady=2)
            tk.Label(row, text=s, font=self.f.body, fg=TEXT, bg=PANEL, wraplength=330,
                     justify="left").pack(side="left", padx=10)

        right = tk.Frame(cols, bg=PANEL, padx=22, pady=18, highlightbackground=LINE,
                         highlightthickness=1)
        right.grid(row=0, column=1, sticky="nsew")
        tk.Label(right, text="Your hand → the arm", font=self.f.h2, fg=TEXT,
                 bg=PANEL).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        gestures = [
            ("Move left / right", "Base turns"),
            ("Move up / down", "Shoulder lifts"),
            ("Move closer / farther", "Elbow reaches"),
            ("Tilt your hand", "Wrist rotates"),
            ("Make a fist", "Gripper closes"),
            ("Open your hand", "Gripper opens"),
        ]
        for r, (g, a) in enumerate(gestures, 1):
            tk.Label(right, text=g, font=self.f.body, fg=TEXT, bg=PANEL).grid(
                row=r, column=0, sticky="w", pady=5)
            tk.Label(right, text=a, font=self.f.body, fg=ACCENT, bg=PANEL).grid(
                row=r, column=1, sticky="w", padx=(28, 0))
        tk.Label(right, text="Shortcuts on the live screen:  Space = pause,  H = home,  Esc = stop",
                 font=self.f.small, fg=MUTED, bg=PANEL).grid(row=8, column=0, columnspan=2,
                                                            sticky="w", pady=(12, 0))


class PlatformPage(Page):
    def __init__(self, app):
        super().__init__(app)
        self.choice = tk.StringVar(value="")
        wrap = tk.Frame(self, bg=BG)
        wrap.place(relx=0.5, rely=0.43, anchor="center")
        tk.Label(wrap, text="Where is this running?", font=self.f.h1, fg=TEXT,
                 bg=BG).pack(anchor="w")
        tk.Label(wrap, text="Pick the device, then choose the arm's USB port.",
                 font=self.f.body, fg=MUTED, bg=BG).pack(anchor="w", pady=(2, 18))

        cards = tk.Frame(wrap, bg=BG)
        cards.pack()
        self.cards = {}
        self._make_card(cards, "pc", "PC / Laptop",
                        "Full screen: live video with the hand's stick model, "
                        "directions and joint readouts.", 0)
        self._make_card(cards, "pi", "Raspberry Pi",
                        "Headless: this window closes and tracking keeps running "
                        "in the terminal, printing status each second.", 1)

        opts = tk.Frame(wrap, bg=BG)
        opts.pack(fill="x", pady=(20, 0))
        tk.Label(opts, text="ARM PORT", font=self.f.label, fg=MUTED, bg=BG).grid(
            row=0, column=0, sticky="w")
        self.port_var = tk.StringVar(value=PREVIEW_ONLY)
        self.port_box = ttk.Combobox(opts, textvariable=self.port_var, state="readonly",
                                     width=38, style="Dark.TCombobox")
        self.port_box.grid(row=1, column=0, sticky="w", pady=(4, 0))
        FlatButton(opts, "Refresh", self.refresh_ports, self.f, kind="secondary",
                   width=100, height=34).grid(row=1, column=1, padx=8, pady=(4, 0))

        tk.Label(opts, text="CAMERA", font=self.f.label, fg=MUTED, bg=BG).grid(
            row=0, column=2, sticky="w", padx=(18, 0))
        self.cam_var = tk.StringVar(value="0")
        tk.Spinbox(opts, from_=0, to=5, textvariable=self.cam_var, width=4, font=self.f.body,
                   bg=PANEL, fg=TEXT, buttonbackground=PANEL_HI, insertbackground=TEXT,
                   relief="flat", highlightthickness=1, highlightbackground=LINE).grid(
            row=1, column=2, sticky="w", padx=(18, 0), pady=(4, 0))

        self.picam_var = tk.BooleanVar(value=False)
        self.picam_check = tk.Checkbutton(
            opts, text="Pi Camera Module", variable=self.picam_var, font=self.f.small,
            fg=TEXT, bg=BG, selectcolor=PANEL, activebackground=BG, activeforeground=TEXT,
            highlightthickness=0, bd=0)
        self.picam_check.grid(row=1, column=3, sticky="w", padx=(14, 0), pady=(4, 0))

        self.choice.trace_add("write", lambda *_: self._update())

    def _make_card(self, parent, key, title, desc, col):
        card = tk.Frame(parent, bg=PANEL, padx=22, pady=20, cursor="hand2",
                        highlightthickness=2, highlightbackground=LINE)
        card.grid(row=0, column=col, padx=(0 if col == 0 else 16, 0), sticky="nsew")
        dot = tk.Label(card, text="○", font=self.f.h1, fg=MUTED, bg=PANEL)
        dot.pack(anchor="w")
        t = tk.Label(card, text=title, font=self.f.h2, fg=TEXT, bg=PANEL)
        t.pack(anchor="w", pady=(4, 6))
        d = tk.Label(card, text=desc, font=self.f.body, fg=MUTED, bg=PANEL,
                     wraplength=300, justify="left")
        d.pack(anchor="w")
        for w in (card, dot, t, d):
            w.bind("<Button-1>", lambda e, k=key: self.choice.set(k))
        self.cards[key] = (card, dot)

    def _update(self):
        sel = self.choice.get()
        for key, (card, dot) in self.cards.items():
            on = key == sel
            card.config(highlightbackground=ACCENT if on else LINE)
            dot.config(text="●" if on else "○", fg=ACCENT if on else MUTED)
        self.picam_check.config(state="normal" if sel == "pi" else "disabled")
        self.app.update_nav()

    def refresh_ports(self):
        ports = [f"{dev}  —  {desc}" for dev, desc in hc.list_serial_ports()]
        self.port_box["values"] = [PREVIEW_ONLY] + ports
        if self.port_var.get() not in self.port_box["values"]:
            # Auto-pick the first thing that looks like an Arduino.
            guess = next((p for p in ports if any(k in p.lower() for k in
                          ("arduino", "acm", "usbmodem", "ch340", "usb serial"))), None)
            self.port_var.set(guess or PREVIEW_ONLY)

    def on_show(self):
        self.refresh_ports()
        self._update()

    def settings(self):
        port = self.port_var.get()
        port = None if port == PREVIEW_ONLY else port.split("  —  ")[0]
        try:
            cam = int(self.cam_var.get())
        except ValueError:
            cam = 0
        return {"mode": self.choice.get(), "port": port, "camera": cam,
                "picam": bool(self.picam_var.get()) and self.choice.get() == "pi"}


class LivePage(Page):
    """Directions on the left, live video + stick model on the right."""

    JOINTS = [
        ("base", "Move left / right"),
        ("shoulder", "Move up / down"),
        ("elbow", "Closer / farther"),
        ("wrist", "Tilt your hand"),
        ("gripper", "Make a fist to grab"),
    ]

    def __init__(self, app):
        super().__init__(app)
        self.worker = None
        self.tracker = None
        self.state = None
        self.error = None
        self.lock = threading.Lock()
        self.running = False
        self.requests = set()
        self._photo = None
        self._poll_id = None

        # ---- left: directions ------------------------------------------------
        left = tk.Frame(self, bg=PANEL, width=360, padx=20, pady=18,
                        highlightbackground=LINE, highlightthickness=1)
        left.pack(side="left", fill="y", padx=(16, 8), pady=16)
        left.pack_propagate(False)

        tk.Label(left, text="Directions", font=self.f.h1, fg=TEXT, bg=PANEL).pack(anchor="w")
        self.hint = tk.Label(left, text="Starting camera…", font=self.f.body, fg=WARM,
                             bg=PANEL, wraplength=315, justify="left", anchor="w")
        self.hint.pack(anchor="w", fill="x", pady=(4, 2))
        self.status = tk.Label(left, text="", font=self.f.small, fg=MUTED, bg=PANEL,
                               anchor="w", justify="left", wraplength=315)
        self.status.pack(anchor="w", fill="x", pady=(0, 12))

        self.rows = {}
        for key, how in self.JOINTS:
            row = tk.Frame(left, bg=PANEL)
            row.pack(fill="x", pady=4)
            top = tk.Frame(row, bg=PANEL)
            top.pack(fill="x")
            tk.Label(top, text=how, font=self.f.body, fg=TEXT,
                     bg=PANEL).pack(side="left")
            val = tk.Label(top, text="--", font=self.f.mono, fg=ACCENT, bg=PANEL)
            val.pack(side="right")
            tk.Label(top, text=key.capitalize(), font=self.f.small, fg=MUTED,
                     bg=PANEL).pack(side="right", padx=8)
            bar = tk.Canvas(row, height=6, bg=PANEL_HI, highlightthickness=0)
            bar.pack(fill="x", pady=(4, 0))
            self.rows[key] = (val, bar)

        tk.Label(left, text="ARM MODEL", font=self.f.label, fg=MUTED,
                 bg=PANEL).pack(anchor="w", pady=(14, 4))
        self.arm_canvas = tk.Canvas(left, height=170, bg=BG, highlightthickness=1,
                                    highlightbackground=LINE)
        self.arm_canvas.pack(fill="x")

        btns = tk.Frame(left, bg=PANEL)
        btns.pack(side="bottom", fill="x")
        self.pause_btn = FlatButton(btns, "Pause", lambda: self.request("pause"), self.f,
                                    kind="secondary", width=100, height=40)
        self.pause_btn.pack(side="left")
        FlatButton(btns, "Home", lambda: self.request("home"), self.f, kind="secondary",
                   width=100, height=40).pack(side="left", padx=8)
        FlatButton(btns, "Stop", self.app.stop_live, self.f, kind="danger",
                   width=100, height=40).pack(side="left")

        # ---- right: video ----------------------------------------------------
        right = tk.Frame(self, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(8, 16), pady=16)
        head = tk.Frame(right, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="Live camera", font=self.f.h2, fg=TEXT, bg=BG).pack(side="left")
        self.fps_label = tk.Label(head, text="", font=self.f.mono, fg=MUTED, bg=BG)
        self.fps_label.pack(side="right")
        self.video = tk.Label(right, bg="#000000", fg=MUTED, font=self.f.body,
                              text="Starting camera…")
        self.video.pack(fill="both", expand=True, pady=(8, 0))

    # ---- worker thread (camera + tracking never block the UI) ---------------
    def start(self, settings):
        self.state, self.error = None, None
        self.running = True
        self.requests.clear()
        self.video.config(image="", text="Starting camera and hand model…")
        self._photo = None
        self.hint.config(text="Starting camera…", fg=WARM)
        self.worker = threading.Thread(target=self._work, args=(settings,), daemon=True)
        self.worker.start()
        self._poll()

    def _work(self, s):
        try:
            self.tracker = hc.ArmTracker(port=s["port"], camera=s["camera"], picam=False,
                                         width=PC_RES[0], height=PC_RES[1])
        except Exception as e:
            self.error = str(e)
            self.running = False
            return
        try:
            while self.running:
                if "pause" in self.requests:
                    self.requests.discard("pause")
                    self.tracker.toggle_pause()
                if "home" in self.requests:
                    self.requests.discard("home")
                    self.tracker.home()
                st = self.tracker.step()
                if st is None:
                    time.sleep(0.01)
                    continue
                hc.draw_skeleton(st["frame"], st["landmarks"], st["fist"])
                with self.lock:
                    self.state = st
        except Exception as e:
            self.error = str(e)
        finally:
            self.tracker.close()
            self.tracker = None

    def request(self, what):
        self.requests.add(what)

    def stop(self):
        self.running = False
        if self._poll_id:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        if self.worker:
            self.worker.join(timeout=3)
            self.worker = None

    # ---- UI refresh -----------------------------------------------------------
    def _poll(self):
        if self.error:
            msg = self.error
            self.error = None
            self.hint.config(text="Couldn't start: " + msg, fg=DANGER)
            self.video.config(image="", text="Camera or arm unavailable.\n"
                                             "Press Stop, check the settings, and try again.")
            self._poll_id = None
            return
        with self.lock:
            st, self.state = self.state, None
        if st is not None:
            self._show(st)
        self._poll_id = self.after(15, self._poll)

    def _show(self, st):
        # Video, scaled to fit while keeping its shape.
        frame = st["frame"]
        bw, bh = max(self.video.winfo_width(), 50), max(self.video.winfo_height(), 50)
        fh, fw = frame.shape[:2]
        scale = min(bw / fw, bh / fh)
        frame = cv2.resize(frame, (max(1, int(fw * scale)), max(1, int(fh * scale))),
                           interpolation=cv2.INTER_LINEAR)
        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        self._photo = ImageTk.PhotoImage(img)
        self.video.config(image=self._photo, text="")
        self.fps_label.config(text=f"{st['fps']:4.1f} FPS")

        # Directions + status.
        has_hand = st["features"] is not None
        if st["paused"]:
            self.hint.config(text="Paused. The arm is frozen. Press Resume (or Space).", fg=WARM)
        elif not has_hand:
            self.hint.config(text="Raise one hand into view, palm facing the camera.", fg=WARM)
        elif st["fist"]:
            self.hint.config(text="Gripping! Open your hand to let go.", fg=WARM)
        else:
            self.hint.config(text="Tracking. Move slowly; make a fist to grab.", fg=ACCENT)
        link = self.app.settings["port"] or "preview only, no arm connected"
        self.status.config(text=f"Hand: {'detected' if has_hand else 'not found'}\n"
                                f"Arm: {link}")
        self.pause_btn.configure_button(text="Resume" if st["paused"] else "Pause")

        for key, (val, bar) in self.rows.items():
            ang = st["angles"][key]
            mn, mx, _home, _inv = hc.SERVOS[key]
            if key == "gripper":
                val.config(text="CLOSED" if st["fist"] else "OPEN",
                           fg=WARM if st["fist"] else ACCENT)
            else:
                val.config(text=f"{int(round(ang)):3d}°", fg=ACCENT if has_hand else MUTED)
            bar.delete("all")
            w = bar.winfo_width()
            t = (ang - mn) / (mx - mn) if mx != mn else 0
            bar.create_rectangle(0, 0, max(2, w * max(0.0, min(1.0, t))), 6,
                                 fill=WARM if (key == "gripper" and st["fist"]) else ACCENT,
                                 width=0)
        self._draw_arm(st["angles"], st["fist"])

    def _draw_arm(self, a, fist):
        """Illustrative side view of the arm plus a top-view dial for the base."""
        c = self.arm_canvas
        c.delete("all")
        w, h = max(c.winfo_width(), 200), max(c.winfo_height(), 150)
        gx, gy = w * 0.38, h - 22
        c.create_line(12, gy, w * 0.72, gy, fill=LINE, width=2)
        c.create_rectangle(gx - 24, gy - 14, gx + 24, gy, fill=PANEL_HI, outline=LINE)
        seg = min(w, h) * 0.36
        sx, sy = gx, gy - 14
        # Servo 90 = upper arm 45° up and forward; forearm bends relative to it.
        e1 = math.radians(a["shoulder"] - 45)
        ex, ey = sx + seg * math.cos(e1), sy - seg * math.sin(e1)
        e2 = e1 - math.radians(150 - a["elbow"])
        wx, wy = ex + seg * 0.85 * math.cos(e2), ey - seg * 0.85 * math.sin(e2)
        for (x1, y1, x2, y2) in ((sx, sy, ex, ey), (ex, ey, wx, wy)):
            c.create_line(x1, y1, x2, y2, fill=ACCENT, width=6, capstyle="round")
        jaw = math.radians(8 if fist else 28)
        jl = 16
        for s in (1, -1):
            ang = e2 + s * jaw
            c.create_line(wx, wy, wx + jl * math.cos(ang), wy - jl * math.sin(ang),
                          fill=WARM if fist else TEXT, width=4, capstyle="round")
        for (x, y) in ((sx, sy), (ex, ey), (wx, wy)):
            c.create_oval(x - 5, y - 5, x + 5, y + 5, fill=TEXT, outline=BG, width=2)
        # Base dial (top view).
        cx, cy, r = w - 42, 40, 26
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=LINE, width=2)
        b = math.radians(180 - a["base"])
        c.create_line(cx, cy, cx + r * math.cos(b), cy - r * math.sin(b), fill=ACCENT,
                      width=3, arrow="last")
        c.create_text(cx, cy + r + 12, text="base", fill=MUTED, font=self.f.small)
        # Wrist roll indicator.
        c.create_text(cx, cy + r + 40, text=f"wrist {int(a['wrist'])}°", fill=MUTED,
                      font=self.f.small)


# --------------------------------------------------------------------------
# App shell: page switching + bottom navigation
# --------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.fonts = Fonts()
        self.settings = {}
        self.launch_headless = None
        root.title("Hand-Controlled Robot Arm")
        root.configure(bg=BG)
        root.geometry("1180x720")
        root.minsize(980, 640)
        self._style()

        self.container = tk.Frame(root, bg=BG)
        self.container.pack(fill="both", expand=True)
        self.pages = [WelcomePage(self), InstructionsPage(self), PlatformPage(self)]
        self.live = LivePage(self)
        self.index = 0

        # Bottom navigation: Back on the left of center, Next on the right of center.
        self.nav = tk.Frame(root, bg=BG, height=90)
        self.nav.pack(fill="x", side="bottom")
        self.back_btn = FlatButton(self.nav, "Back", self.back, self.fonts, kind="secondary")
        self.next_btn = FlatButton(self.nav, "Next  →", self.next, self.fonts, kind="primary",
                                   width=190)
        self.back_btn.place(relx=0.32, rely=0.45, anchor="center")
        self.next_btn.place(relx=0.68, rely=0.45, anchor="center")
        self.dots = tk.Canvas(self.nav, width=80, height=14, bg=BG, highlightthickness=0)
        self.dots.place(relx=0.5, rely=0.45, anchor="center")

        root.bind("<space>", lambda e: self._live_key("pause"))
        root.bind("<Key-h>", lambda e: self._live_key("home"))
        root.bind("<Escape>", lambda e: self.stop_live() if self.live.winfo_ismapped() else None)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.show(0)

    def _style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("Dark.TCombobox", fieldbackground=PANEL, background=PANEL_HI,
                    foreground=TEXT, arrowcolor=TEXT, bordercolor=LINE, lightcolor=PANEL,
                    darkcolor=PANEL, padding=6)
        s.map("Dark.TCombobox", fieldbackground=[("readonly", PANEL)],
              foreground=[("readonly", TEXT)], selectbackground=[("readonly", PANEL)],
              selectforeground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_DK)

    def show(self, i):
        for p in self.pages:
            p.pack_forget()
        self.live.pack_forget()
        self.index = i
        self.pages[i].pack(in_=self.container, fill="both", expand=True)
        self.pages[i].on_show()
        if not self.nav.winfo_ismapped():
            self.nav.pack(fill="x", side="bottom")
        self.update_nav()

    def update_nav(self):
        last = self.index == len(self.pages) - 1
        self.back_btn.configure_button(enabled=self.index > 0)
        if last:
            mode = self.pages[-1].choice.get()
            label = {"pc": "Start  →", "pi": "Go headless  →"}.get(mode, "Choose a device")
            self.next_btn.configure_button(text=label, enabled=bool(mode))
        else:
            self.next_btn.configure_button(text="Next  →", enabled=True)
        self.dots.delete("all")
        n = len(self.pages)
        for k in range(n):
            x = 40 + (k - (n - 1) / 2) * 20
            self.dots.create_oval(x - 4, 3, x + 4, 11, width=0,
                                  fill=ACCENT if k == self.index else LINE)

    def back(self):
        if self.index > 0:
            self.show(self.index - 1)

    def next(self):
        if self.index < len(self.pages) - 1:
            self.show(self.index + 1)
            return
        self.settings = self.pages[-1].settings()
        if self.settings["mode"] == "pi":
            self.launch_headless = self.settings
            self.root.destroy()          # main() picks this up and runs headless
        elif self.settings["mode"] == "pc":
            self.start_live()

    def start_live(self):
        self.pages[self.index].pack_forget()
        self.nav.pack_forget()
        self.live.pack(in_=self.container, fill="both", expand=True)
        self.root.update_idletasks()
        self.live.start(self.settings)

    def stop_live(self):
        self.live.stop()
        self.show(len(self.pages) - 1)

    def _live_key(self, what):
        if self.live.winfo_ismapped():
            self.live.request(what)

    def quit(self):
        self.live.stop()
        self.root.destroy()


# --------------------------------------------------------------------------
def has_display():
    if platform.system() in ("Windows", "Darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def run_pi_headless(port, camera=0, picam=False):
    print("\n=== Raspberry Pi mode: headless monitoring ===")
    print(f"Arm port: {port or 'none (preview only)'}   Camera: "
          f"{'Pi Camera Module' if picam else camera}")
    try:
        tracker = hc.ArmTracker(port=port, camera=camera, picam=picam,
                                width=PI_RES[0], height=PI_RES[1])
    except Exception as e:
        sys.exit(f"Startup failed: {e}")
    hc.run_headless(tracker)


def main():
    ap = argparse.ArgumentParser(description="Hand-Controlled Robot Arm app")
    ap.add_argument("--headless", action="store_true",
                    help="skip the window and run Raspberry Pi mode straight away")
    ap.add_argument("--port", default=None, help="arm port for --headless, e.g. /dev/ttyACM0")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--picam", action="store_true")
    args = ap.parse_args()

    if args.headless or not has_display():
        if not args.headless:
            print("No display found, so starting in headless (Raspberry Pi) mode.")
        run_pi_headless(args.port, args.camera, args.picam)
        return

    root = tk.Tk()
    app = App(root)
    root.mainloop()

    if app.launch_headless:
        s = app.launch_headless
        run_pi_headless(s["port"], s["camera"], s["picam"])


if __name__ == "__main__":
    main()
