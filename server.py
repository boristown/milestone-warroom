#!/usr/bin/env python3
"""Ultra-lightweight, zero-dependency, auth-gated War-Room server with live SSE experiment streaming."""
from __future__ import annotations
import datetime
import glob
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import re
import socketserver
import time
import urllib.parse

HOST = os.environ.get("WARROOM_HOST", "127.0.0.1")
PORT = int(os.environ.get("WARROOM_PORT", "13555"))
BASE_PATH = os.environ.get("WARROOM_BASE_PATH", "").rstrip("/")
TOKEN = os.environ.get("WARROOM_TOKEN", "")
TEAM_NAME = os.environ.get("WARROOM_TEAM_NAME", "Team Horizon")
DATA_DIR = os.environ.get("DATA_DIR", ".")
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

MILESTONES_FILE = os.environ.get("MILESTONES_FILE", os.path.join(DATA_DIR, "milestones.json"))
EXPERIMENTS_FILE = os.environ.get("EXPERIMENTS_FILE", os.path.join(DATA_DIR, "experiments.jsonl"))
RADAR_FILE = os.environ.get("RADAR_FILE", os.path.join(DATA_DIR, "radar_latest.json"))
STATUS_FILE = os.environ.get("STATUS_FILE", os.path.join(DATA_DIR, "commander_status.md"))
TRANSCRIPT_GLOB = os.environ.get(
    "TRANSCRIPT_GLOB",
    "",
)

COOKIE_NAME = "warroom_sess"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def make_session_sig(token: str) -> str:
    return hmac.new(token.encode("utf-8"), b"milestone-warroom-v2", hashlib.sha256).hexdigest()


def parse_status_telemetry(fleet: dict) -> dict:
    out = dict(fleet or {})
    if STATUS_FILE and os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            m_cu = re.search(r"CU Balance[^0-9]*([0-9,]+\.[0-9]+)\s*CU", txt)
            if m_cu:
                out["cu_balance"] = float(m_cu.group(1).replace(",", ""))
            m_burn = re.search(r"([0-9]+\.[0-9]+)\s*CU/hr", txt)
            if m_burn:
                out["cu_burn_per_hr"] = float(m_burn.group(1))
            m_rem = re.search(r"Daily Quota\s*\((\d{4}-\d{2}-\d{2})\s*UTC\)[^0-9]*([0-9]+)\s*/\s*([0-9]+)\s*remaining", txt)
            if m_rem:
                q_day, rem, tot = m_rem.group(1), int(m_rem.group(2)), int(m_rem.group(3))
                out["quota_day_utc"] = q_day
                out["quota_total"] = tot
                out["quota_used"] = max(0, tot - rem)
        except OSError:
            pass
    today_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if out.get("quota_day_utc") and out["quota_day_utc"] < today_utc:
        out["quota_day_utc"] = today_utc
        out["quota_used"] = 0
    return out


def load_radar_summary(pb_score: float) -> dict:
    summary = {
        "checked_utc": None,
        "top15": [],
        "our_rank": None,
        "our_score": pb_score,
        "rank1": None,
        "rank3": None,
        "rank10": None,
    }
    if not RADAR_FILE or not os.path.exists(RADAR_FILE):
        return summary
    try:
        with open(RADAR_FILE, "r", encoding="utf-8") as f:
            r = json.load(f)
        summary["checked_utc"] = r.get("checked_utc")
        for row in (r.get("lb_top", []) or [])[:15]:
            sc = float(row.get("score", 0))
            item = {
                "rank": int(row.get("rank", 0)),
                "team": row.get("team", ""),
                "score": sc,
                "is_us": bool(TEAM_NAME and TEAM_NAME in str(row.get("team", ""))),
            }
            summary["top15"].append(item)
            if item["rank"] == 1:
                summary["rank1"] = item
            elif item["rank"] == 3:
                summary["rank3"] = item
            elif item["rank"] == 10:
                summary["rank10"] = item
            if item["is_us"]:
                summary["our_rank"] = item["rank"]
                summary["our_score"] = sc
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return summary


def clean_result_line(content: str) -> tuple[str, str]:
    """Return (status: pass|fail|trying, 1-line summary) from a tool execution response."""
    txt = ANSI_RE.sub("", content or "")
    if "Tool is running as a background task" in txt:
        return "trying", "后台运行中..."
    m_code = re.search(r"Command exited with code (\d+)", txt)
    exit_code = int(m_code.group(1)) if m_code else 0

    out_body = txt.split("Command output:", 1)[1] if "Command output:" in txt else txt
    lines = []
    for raw in out_body.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith(("Created At:", "Completed At:", "Log output:", "Task logs are")):
            continue
        lines.append(s)

    fail_pat = re.compile(r"\b(Traceback|Error:|Exception|AssertionError|FAIL|FAILED)\b")
    is_fail = exit_code != 0 or any(fail_pat.search(l) for l in lines[-4:])
    status = "fail" if is_fail else "pass"

    # Prefer the most informative metric/result line (last meaningful non-boilerplate line)
    pick = ""
    metric_pat = re.compile(r"(PASS|FAIL|EXACT|->|=>|mean|Tof|B\b|GM|peak|Q=|err|hits|rank|FULL|DONE|OK)", re.I)
    for l in reversed(lines[-8:]):
        if metric_pat.search(l):
            pick = l
            break
    if not pick and lines:
        pick = lines[-1]
    if not pick:
        pick = f"exit={exit_code}" if exit_code else "OK"
    return status, pick[:120]


def classify_stream(sample_text: str) -> tuple[str, str]:
    """Map transcript text to (engine_id, worker_tag); override via experiments.jsonl for custom routing."""
    low = sample_text.lower()
    if "engine1" in low or "worker-c" in low:
        return "engine1", "W3"
    if "engine2" in low or "worker-a" in low or "worker-b" in low:
        return "engine2", "W1"
    return "engine2", "AG"


def tail_live_transcripts(max_per_engine: int = 8) -> dict[str, list[dict]]:
    """Fast tail (<4ms) of active specialist transcripts into real-time try -> pass/fail rows."""
    by_engine: dict[str, list[dict]] = {}
    if not TRANSCRIPT_GLOB:
        return by_engine

    now_ts = time.time()
    paths = sorted(glob.glob(TRANSCRIPT_GLOB), key=lambda p: os.path.getmtime(p), reverse=True)
    for p in paths[:6]:
        try:
            if now_ts - os.path.getmtime(p) > 7200:
                continue
            with open(p, "rb") as f:
                head = f.readline().decode("utf-8", "ignore")
                if '"USER_INPUT"' in head:
                    continue  # Skip commander root; focus on specialist workers
                f.seek(0, 2)
                sz = f.tell()
                f.seek(max(0, sz - 96000))
                raw_lines = f.read().decode("utf-8", "ignore").splitlines()
                if sz > 96000 and raw_lines:
                    raw_lines = raw_lines[1:]
        except OSError:
            continue

        sample = "\n".join(raw_lines[-20:])
        eng_id, worker = classify_stream(sample)
        if eng_id == "global":
            continue

        parsed = []
        for ln in raw_lines[-45:]:
            try:
                parsed.append(json.loads(ln))
            except Exception:
                continue

        items: list[dict] = []
        i = 0
        while i < len(parsed):
            o = parsed[i]
            tcs = o.get("tool_calls") or []
            ts = str(o.get("created_at", ""))
            # Capture run_command attempts paired with their immediate result
            if o.get("type") == "PLANNER_RESPONSE" and tcs:
                for tc in tcs:
                    if tc.get("name") != "run_command":
                        continue
                    args = tc.get("args") or tc.get("arguments") or {}
                    summary = str(args.get("toolSummary", "")).strip().strip('"')
                    if not summary:
                        continue
                    status = "trying"
                    result = "执行中..."
                    # Look ahead 1-2 steps for the corresponding GENERIC output
                    if i + 1 < len(parsed) and parsed[i + 1].get("type") == "GENERIC":
                        nxt = parsed[i + 1]
                        if nxt.get("status") == "RUNNING":
                            status, result = "trying", "TPU 运行中..."
                        else:
                            status, result = clean_result_line(str(nxt.get("content", "")))
                            ts = str(nxt.get("created_at", ts))
                    items.append({
                        "utc": ts,
                        "worker": worker,
                        "status": status,
                        "action": summary[:92],
                        "result": result,
                    })
            elif o.get("type") == "PLANNER_RESPONSE" and not tcs:
                txt = str(o.get("content", "")).strip()
                if txt and len(txt) < 220 and not txt.startswith("Standing by"):
                    first_ln = txt.splitlines()[0].replace("`", "").strip()
                    if first_ln:
                        items.append({
                            "utc": ts,
                            "worker": worker,
                            "status": "trying" if "Waiting" in first_ln else "info",
                            "action": first_ln[:110],
                            "result": "",
                        })
            i += 1

        by_engine.setdefault(eng_id, []).extend(items[-max_per_engine:])

    # Also merge optional manual structured experiments.jsonl
    if EXPERIMENTS_FILE and os.path.exists(EXPERIMENTS_FILE):
        try:
            with open(EXPERIMENTS_FILE, "r", encoding="utf-8") as f:
                for ln in f.readlines()[-30:]:
                    ln = ln.strip()
                    if not ln:
                        continue
                    obj = json.loads(ln)
                    eng_id = obj.get("engine", "engine2")
                    by_engine.setdefault(eng_id, []).append({
                        "utc": obj.get("utc", ""),
                        "worker": obj.get("worker", "CMD"),
                        "status": obj.get("status", "info"),
                        "action": str(obj.get("action", ""))[:100],
                        "result": str(obj.get("result", ""))[:120],
                    })
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    for eng_id, rows in by_engine.items():
        rows.sort(key=lambda x: x.get("utc", ""), reverse=True)
        by_engine[eng_id] = rows[:max_per_engine]
    return by_engine


def build_state() -> dict:
    with open(MILESTONES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    now = datetime.datetime.now(datetime.timezone.utc)
    next_reset = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    reset_in_sec = int((next_reset - now).total_seconds())

    fleet = parse_status_telemetry(data.get("fleet", {}))
    radar = load_radar_summary(float(fleet.get("private_pb_score", 0.0)))
    live_streams = tail_live_transcripts(max_per_engine=7)

    engines = []
    for eng in data.get("engines", []):
        e = dict(eng)
        e.pop("workspace_paths", None)
        e["stream"] = live_streams.get(e.get("id", ""), [])
        engines.append(e)

    return {
        "server_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reset_in_sec": reset_in_sec,
        "updated_utc": data.get("updated_utc"),
        "mission_title": data.get("mission_title", "WAR-ROOM"),
        "policy_badge": data.get("policy_badge", ""),
        "fleet": fleet,
        "single_projection": data.get("single_projection", {}),
        "combo_projection": data.get("combo_projection", {}),
        "engines": engines,
        "radar": radar,
    }


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class WarRoomHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _is_authenticated(self, parsed) -> bool:
        if not TOKEN:
            return False
        qs = urllib.parse.parse_qs(parsed.query)
        q_tok = (qs.get("k") or [""])[0]
        if q_tok and hmac.compare_digest(q_tok, TOKEN):
            return True
        hdr_tok = self.headers.get("X-Warroom-Token", "")
        if hdr_tok and hmac.compare_digest(hdr_tok, TOKEN):
            return True
        raw_cookie = self.headers.get("Cookie", "")
        if raw_cookie:
            c = http.cookies.SimpleCookie()
            try:
                c.load(raw_cookie)
                if COOKIE_NAME in c and hmac.compare_digest(c[COOKIE_NAME].value, make_session_sig(TOKEN)):
                    return True
            except http.cookies.CookieError:
                pass
        return False

    def _cookie_header(self) -> str:
        path = (BASE_PATH + "/") if BASE_PATH else "/"
        sig = make_session_sig(TOKEN)
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return f"{COOKIE_NAME}={sig}; Path={path}; HttpOnly{secure}; SameSite=Lax; Max-Age=2592000"

    def _send_json(self, code: int, payload: dict, extra_headers: dict | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path == "/health":
            self._send_json(200, {"ok": True})
            return

        if path in ("/", "/index.html"):
            qs = urllib.parse.parse_qs(parsed.query)
            q_tok = (qs.get("k") or [""])[0]
            if TOKEN and q_tok and hmac.compare_digest(q_tok, TOKEN):
                dest = (BASE_PATH + "/") if BASE_PATH else "/"
                self.send_response(302)
                self.send_header("Set-Cookie", self._cookie_header())
                self.send_header("Location", dest)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                html = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if path == "/api/state":
            if not self._is_authenticated(parsed):
                self._send_json(401, {"error": "unauthorized"})
                return
            self._send_json(200, build_state())
            return

        if path == "/api/stream":
            if not self._is_authenticated(parsed):
                self._send_json(401, {"error": "unauthorized"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
            self.end_headers()
            last_sig = ""
            ticks = 0
            try:
                while True:
                    st = build_state()
                    # Hash excluding server_utc & reset_in_sec so we only push real changes (or every 15s for timers)
                    sig_obj = {k: v for k, v in st.items() if k not in ("server_utc", "reset_in_sec")}
                    sig = hashlib.md5(json.dumps(sig_obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
                    if sig != last_sig or ticks % 6 == 0:
                        payload = json.dumps(st, ensure_ascii=False)
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        last_sig = sig
                    else:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    ticks += 1
                    time.sleep(2.5)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/api/auth":
            self._send_json(404, {"error": "not_found"})
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            key = str(body.get("key", "")).strip()
        except Exception:
            self._send_json(400, {"error": "bad_request"})
            return
        if TOKEN and hmac.compare_digest(key, TOKEN):
            self._send_json(200, {"ok": True}, extra_headers={"Set-Cookie": self._cookie_header()})
        else:
            self._send_json(401, {"error": "invalid_key"})


def main():
    if not TOKEN:
        raise RuntimeError("WARROOM_TOKEN must be set")
    srv = ThreadingHTTPServer((HOST, PORT), WarRoomHandler)
    print(f"warroom listening on http://{HOST}:{PORT} (base={BASE_PATH or '/'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
