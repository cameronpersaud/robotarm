#!/usr/bin/env python3
"""
Session logging to Tiger Data (Tiger Cloud / TimescaleDB).

Every session records, 10 times a second, the arm's joint angles and the raw hand
measurements, plus events (grips, floor limits, hand lost...). That turns the arm
into a rehab tool: range of motion, grip count and active time per session, and
progress compared with the previous session.

Tables (created automatically on first connect):
  arm_sessions   one row per session
  arm_telemetry  hypertable, 10 Hz samples
  arm_events     hypertable, discrete events
  arm_motion_1m  continuous aggregate: range of motion per minute

Setup: put your Tiger Cloud service URL in config.env as TIGER_DATABASE_URL.
Report: python3 telemetry.py              (recent sessions + per-minute trend)
        python3 telemetry.py --setup      (just create the tables)
"""

import argparse
import collections
import platform
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import hand_control as hc

SAMPLE_HZ = 10
FLUSH_EVERY_S = 1.0
RECONNECT_EVERY_S = 5.0
MAX_BUFFER_ROWS = 30 * 60 * SAMPLE_HZ      # 30 minutes kept in memory if the DB is unreachable

JOINTS = hc.JOINT_ORDER
TELEMETRY_COLS = (["time", "session_id"] + JOINTS +
                  ["hand_detected", "fist", "floor_limited", "paused", "fps",
                   "hand_x", "hand_y", "hand_size", "hand_roll", "hand_open"])

SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS arm_sessions (
    session_id  UUID PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL,
    ended_at    TIMESTAMPTZ,
    device      TEXT,
    user_label  TEXT
);
CREATE TABLE IF NOT EXISTS arm_telemetry (
    time          TIMESTAMPTZ NOT NULL,
    session_id    UUID NOT NULL,
    base REAL, shoulder REAL, elbow REAL, wrist REAL, gripper REAL,
    hand_detected BOOLEAN, fist BOOLEAN, floor_limited BOOLEAN, paused BOOLEAN,
    fps REAL,
    hand_x REAL, hand_y REAL, hand_size REAL, hand_roll REAL, hand_open REAL
);
CREATE TABLE IF NOT EXISTS arm_events (
    time        TIMESTAMPTZ NOT NULL,
    session_id  UUID NOT NULL,
    event       TEXT NOT NULL
);
"""

SCHEMA_TIMESCALE = [
    "SELECT create_hypertable('arm_telemetry', by_range('time'), if_not_exists => TRUE)",
    "SELECT create_hypertable('arm_events', by_range('time'), if_not_exists => TRUE)",
    "CREATE INDEX IF NOT EXISTS arm_telemetry_session_idx ON arm_telemetry (session_id, time DESC)",
    "CREATE INDEX IF NOT EXISTS arm_events_session_idx ON arm_events (session_id, time DESC)",
    """CREATE MATERIALIZED VIEW IF NOT EXISTS arm_motion_1m
       WITH (timescaledb.continuous) AS
       SELECT time_bucket(INTERVAL '1 minute', time) AS bucket,
              session_id,
              max(base)     FILTER (WHERE hand_detected) - min(base)     FILTER (WHERE hand_detected) AS base_range,
              max(shoulder) FILTER (WHERE hand_detected) - min(shoulder) FILTER (WHERE hand_detected) AS shoulder_range,
              max(elbow)    FILTER (WHERE hand_detected) - min(elbow)    FILTER (WHERE hand_detected) AS elbow_range,
              max(wrist)    FILTER (WHERE hand_detected) - min(wrist)    FILTER (WHERE hand_detected) AS wrist_range,
              count(*) FILTER (WHERE hand_detected) AS tracked_samples,
              count(*) FILTER (WHERE floor_limited) AS floor_samples,
              count(*) AS samples
       FROM arm_telemetry
       GROUP BY bucket, session_id
       WITH NO DATA""",
    """SELECT add_continuous_aggregate_policy('arm_motion_1m',
         start_offset => INTERVAL '7 days', end_offset => INTERVAL '1 minute',
         schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE)""",
]


def database_url():
    return hc.get_setting("TIGER_DATABASE_URL")


def connect(url=None):
    import psycopg
    url = url or database_url()
    if not url:
        raise RuntimeError("TIGER_DATABASE_URL is not set (add it to config.env)")
    return psycopg.connect(url, autocommit=True, connect_timeout=10,
                           application_name="hand-robot-arm")


def setup_schema(conn):
    """Creates tables (idempotent). Returns True if TimescaleDB features are active."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA_TABLES)
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        if cur.fetchone() is None:
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            except Exception:
                print("[tiger] TimescaleDB extension not available: logging to plain tables")
                return False
        for stmt in SCHEMA_TIMESCALE:
            cur.execute(stmt)
    return True


def _now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Live logger (tracker listener)
# --------------------------------------------------------------------------
class TigerLogger:
    """Batches samples in memory and writes them from a background thread.

    The arm never waits on the network: if Tiger Cloud is slow or unreachable,
    rows are buffered and written once the connection comes back.
    """

    name = "tiger"

    def __init__(self, url=None, device=None, user_label=None):
        self.url = url or database_url()
        self.session_id = uuid.uuid4()
        self.started_at = _now()
        self.device = device or platform.node() or "unknown"
        self.user_label = (user_label or "").strip() or None
        self.rows = collections.deque(maxlen=MAX_BUFFER_ROWS)
        self.events = collections.deque(maxlen=MAX_BUFFER_ROWS)
        self.written = 0
        self.state_text = "connecting"
        self.last_error = None
        self.timescale = False
        self._conn = None
        self._session_saved = False
        self._last_sample = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---- called from the tracking loop (must be fast) -------------------------
    def handle(self, state):
        now = _now()
        for event in state.get("events", ()):
            self.events.append((now, self.session_id, event))
        mono = time.monotonic()
        if mono - self._last_sample < 1.0 / SAMPLE_HZ:
            return
        self._last_sample = mono
        f = state["features"] or {}
        a = state["angles"]
        self.rows.append((now, self.session_id, *(float(a[j]) for j in JOINTS),
                          bool(state["features"]), bool(state["fist"]),
                          bool(state["floor_limited"]), bool(state["paused"]),
                          float(state["fps"]),
                          f.get("x"), f.get("y"), f.get("size"), f.get("roll"),
                          f.get("openness")))

    def status(self):
        with self._lock:
            pending = len(self.rows)
            if self.state_text == "connected":
                return f"connected, {self.written:,} rows saved"
            if self.state_text == "connecting":
                return "connecting..."
            return f"offline, {pending:,} rows buffered ({self.last_error})"

    # ---- background writer ------------------------------------------------------
    def _ensure_connection(self):
        if self._conn is not None and not self._conn.closed:
            return True
        try:
            self._conn = connect(self.url)
            self.timescale = setup_schema(self._conn)
            if not self._session_saved:
                with self._conn.cursor() as cur:
                    cur.execute("INSERT INTO arm_sessions (session_id, started_at, device, user_label) "
                                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                                (self.session_id, self.started_at, self.device, self.user_label))
                self._session_saved = True
            with self._lock:
                self.state_text, self.last_error = "connected", None
            return True
        except Exception as e:
            self._drop_connection(e)
            return False

    def _drop_connection(self, error):
        text = str(error).strip()
        if type(error).__name__ == "OperationalError":
            text = "can't reach the database (check Wi-Fi / TIGER_DATABASE_URL)"
        elif text:
            text = text.splitlines()[0][:80]
        with self._lock:
            self.state_text = "offline"
            self.last_error = text or type(error).__name__
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def _flush(self):
        if not self.rows and not self.events:
            return True
        if not self._ensure_connection():
            return False
        rows = [self.rows.popleft() for _ in range(len(self.rows))]
        events = [self.events.popleft() for _ in range(len(self.events))]
        try:
            with self._conn.cursor() as cur:
                if rows:
                    with cur.copy(f"COPY arm_telemetry ({', '.join(TELEMETRY_COLS)}) FROM STDIN") as cp:
                        for r in rows:
                            cp.write_row(r)
                if events:
                    with cur.copy("COPY arm_events (time, session_id, event) FROM STDIN") as cp:
                        for r in events:
                            cp.write_row(r)
            with self._lock:
                self.written += len(rows)
            return True
        except Exception as e:
            # Put the batch back (oldest first) so nothing is lost, then retry later.
            self.rows.extendleft(reversed(rows))
            self.events.extendleft(reversed(events))
            self._drop_connection(e)
            return False

    def _run(self):
        self._ensure_connection()
        next_try = 0.0
        while not self._stop.wait(FLUSH_EVERY_S):
            if self._conn is None and time.monotonic() < next_try:
                continue
            if not self._flush():
                next_try = time.monotonic() + RECONNECT_EVERY_S

    # ---- end of session -----------------------------------------------------------
    def close(self, timeout=10.0):
        """Final flush, mark the session ended, and return its summary (or None)."""
        self._stop.set()
        self._thread.join(timeout=timeout)
        deadline = time.monotonic() + timeout
        while (self.rows or self.events) and time.monotonic() < deadline:
            if not self._flush():
                time.sleep(0.5)
        if self.rows or self.events:
            print(f"[tiger] could not save {len(self.rows)} rows: {self.last_error}")
            return {"error": self.last_error, "unsaved_rows": len(self.rows)}
        try:
            with self._conn.cursor() as cur:
                cur.execute("UPDATE arm_sessions SET ended_at = %s WHERE session_id = %s",
                            (_now(), self.session_id))
            summary = session_summary(self._conn, self.session_id)
            summary["previous"] = previous_session_summary(self._conn, self.session_id)
            return summary
        except Exception as e:
            return {"error": str(e).splitlines()[0][:120] if str(e) else type(e).__name__}
        finally:
            try:
                self._conn.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------
def session_summary(conn, session_id):
    """Computed inside Tiger Data from the raw samples of one session."""
    ranges = ", ".join(
        f"max({j}) FILTER (WHERE hand_detected) - min({j}) FILTER (WHERE hand_detected)"
        for j in ("base", "shoulder", "elbow", "wrist"))
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT s.started_at, s.ended_at, s.user_label,
                   EXTRACT(EPOCH FROM coalesce(s.ended_at, max(t.time)) - s.started_at),
                   count(t.*) FILTER (WHERE t.hand_detected)::float / {SAMPLE_HZ},
                   count(t.*), {ranges}
            FROM arm_sessions s
            LEFT JOIN arm_telemetry t
                   ON t.session_id = s.session_id AND t.time >= s.started_at
            WHERE s.session_id = %s
            GROUP BY s.session_id""", (session_id,))
        row = cur.fetchone()
        if row is None:
            return {"error": "session not found"}
        cur.execute("SELECT event, count(*) FROM arm_events WHERE session_id = %s GROUP BY event",
                    (session_id,))
        events = dict(cur.fetchall())
    started, ended, label, duration, tracked, samples, *rng = row
    return {
        "session_id": str(session_id), "started_at": started, "label": label,
        "duration_s": float(duration or 0), "tracked_s": float(tracked or 0),
        "samples": samples, "grips": events.get("grip", 0),
        "floor_limits": events.get("floor_limit", 0), "hand_lost": events.get("hand_lost", 0),
        "range": {j: (float(v) if v is not None else None)
                  for j, v in zip(("base", "shoulder", "elbow", "wrist"), rng)},
    }


def previous_session_summary(conn, session_id):
    """Summary of the session before this one (same label if one was given)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.session_id FROM arm_sessions p, arm_sessions c
            WHERE c.session_id = %s AND p.started_at < c.started_at
              AND p.session_id <> c.session_id
              AND (c.user_label IS NULL OR p.user_label = c.user_label)
            ORDER BY p.started_at DESC LIMIT 1""", (session_id,))
        row = cur.fetchone()
    return session_summary(conn, row[0]) if row else None


def _clock(seconds):
    seconds = int(round(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


def format_summary(s):
    """Plain-text summary used by the app popup and the terminal."""
    if not s:
        return "No session data."
    if "error" in s and "range" not in s:
        return f"Session could not be saved to Tiger Data: {s['error']}"
    prev = s.get("previous")
    lines = ["Session saved to Tiger Data" + (f" ({s['label']})" if s.get("label") else ""),
             f"Duration {_clock(s['duration_s'])}   Hand tracked {_clock(s['tracked_s'])}",
             f"Grips {s['grips']}   Floor limits {s['floor_limits']}   Hand lost {s['hand_lost']}",
             "", "Range of motion" + ("   (vs previous session)" if prev else "")]
    for j, v in s["range"].items():
        if v is None:
            lines.append(f"  {j:9s}  --")
            continue
        text = f"  {j:9s} {v:5.0f}°"
        pv = prev["range"].get(j) if prev else None
        if pv is not None:
            text += f"   ({v - pv:+.0f}°)"
        lines.append(text)
    if prev:
        lines.append(f"\nGrips last session: {prev['grips']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Report (command line)
# --------------------------------------------------------------------------
def report(limit=8):
    conn = connect()
    ts = setup_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT session_id FROM arm_sessions ORDER BY started_at DESC LIMIT %s", (limit,))
        ids = [r[0] for r in cur.fetchall()]
    if not ids:
        print("No sessions yet.")
        return
    print(f"{'started':17s} {'label':10s} {'time':>6s} {'tracked':>7s} {'grips':>5s} "
          f"{'base':>5s} {'shoul':>5s} {'elbow':>5s} {'wrist':>5s}")
    for sid in reversed(ids):
        s = session_summary(conn, sid)
        r = s["range"]
        cell = lambda v: f"{v:5.0f}" if v is not None else "   --"
        print(f"{s['started_at'].astimezone():%Y-%m-%d %H:%M} {(s['label'] or '-')[:10]:10s} "
              f"{_clock(s['duration_s']):>6s} {_clock(s['tracked_s']):>7s} {s['grips']:5d} "
              f"{cell(r['base'])} {cell(r['shoulder'])} {cell(r['elbow'])} {cell(r['wrist'])}")
    if ts:
        with conn.cursor() as cur:
            cur.execute("CALL refresh_continuous_aggregate('arm_motion_1m', NULL, NULL)")
            cur.execute("""SELECT bucket, shoulder_range, elbow_range, tracked_samples, samples
                           FROM arm_motion_1m WHERE session_id = %s ORDER BY bucket""", (ids[0],))
            rows = cur.fetchall()
        if rows:
            print("\nLatest session, minute by minute (continuous aggregate arm_motion_1m):")
            for bucket, sh, el, tracked, total in rows:
                fmt = lambda v: f"{v:4.0f}°" if v is not None else "   --"
                print(f"  {bucket.astimezone():%H:%M}  shoulder {fmt(sh)}  elbow {fmt(el)}  "
                      f"tracked {100 * tracked / max(total, 1):3.0f}%")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Tiger Data logging for the robot arm")
    ap.add_argument("--setup", action="store_true", help="create the tables and exit")
    ap.add_argument("--limit", type=int, default=8, help="sessions to show in the report")
    args = ap.parse_args()
    try:
        if args.setup:
            c = connect()
            print("TimescaleDB features:", "on" if setup_schema(c) else "off (plain Postgres)")
            c.close()
            print("Tables ready.")
        else:
            report(args.limit)
    except Exception as e:
        sys.exit(f"Tiger Data error: {e}")
