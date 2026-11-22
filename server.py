#!/usr/bin/env python3
"""Minimalist, zero-dependency, auth-gated Milestone War-Room telemetry server."""
from __future__ import annotations
import datetime
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import re
import socketserver
import urllib.parse

HOST = os.environ.get("WARROOM_HOST", "127.0.0.1")
PORT = int(os.environ.get("WARROOM_PORT", "13555"))
BASE_PATH = os.environ.get("WARROOM_BASE_PATH", "").rstrip("/")
TOKEN = os.environ.get("WARROOM_TOKEN", "")
TEAM_NAME = os.environ.get("WARROOM_TEAM_NAME", "Team Horizon")
DATA_DIR = os.environ.get("DATA_DIR", ".")
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

MILESTONES_FILE = os.environ.get("MILESTONES_FILE", os.path.join(DATA_DIR, "milestones.json"))
RADAR_FILE = os.environ.get("RADAR_FILE", "")
STATUS_FILE = os.environ.get("STATUS_FILE", "")
ANOMALIES_FILE = os.environ.get("ANOMALIES_FILE", "")

COOKIE_NAME = "warroom_sess"


def make_session_sig(token: str) -> str:
    return hmac.new(token.encode("utf-8"), b"milestone-warroom-v1", hashlib.sha256).hexdigest()


def parse_status_telemetry(fleet: dict) -> dict:
    """Optionally overlay fresh compute/quota numbers from a markdown status file."""
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
            m_rem = re.search(r"Daily[^0-9]*Quota\s*\((\d{4}-\d{2}-\d{2})\s*UTC\)[^0-9]*([0-9]+)\s*/\s*([0-9]+)\s*remaining", txt)
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


def workspace_heartbeat(paths: list[str]) -> tuple[str | None, int | None]:
    """Return latest mtime across top-level files of tracked workspaces (read-only, <2ms)."""
    latest_ts = 0.0
    for p in paths or []:
        if not os.path.exists(p):
            continue
        try:
            st = os.stat(p)
            latest_ts = max(latest_ts, st.st_mtime)
            if os.path.isdir(p):
                with os.scandir(p) as it:
                    for entry in it:
                        if entry.name.startswith("."):
                            continue
                        try:
                            latest_ts = max(latest_ts, entry.stat().st_mtime)
                        except OSError:
                            pass
        except OSError:
            pass
    if latest_ts <= 0:
        return None, None
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    age = max(0, int(now_ts - latest_ts))
    iso = datetime.datetime.fromtimestamp(latest_ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return iso, age


def load_radar_summary(pb_score: float) -> dict:
    summary = {
        "checked_utc": None,
        "top15": [],
        "our_rank": None,
        "our_score": pb_score,
        "rank1": None,
        "rank3": None,
        "rank10": None,
        "gap_to_1": None,
        "gap_to_3": None,
        "gap_to_10": None,
        "anomalies": [],
    }
    if RADAR_FILE and os.path.exists(RADAR_FILE):
        try:
            with open(RADAR_FILE, "r", encoding="utf-8") as f:
                r = json.load(f)
            summary["checked_utc"] = r.get("checked_utc")
            lb = r.get("lb_top", []) or []
            top15 = []
            for row in lb[:15]:
                sc = float(row.get("score", 0))
                item = {
                    "rank": int(row.get("rank", 0)),
                    "team": row.get("team", ""),
                    "score": sc,
                    "date": str(row.get("date", ""))[:16],
                    "is_us": bool(TEAM_NAME and TEAM_NAME in str(row.get("team", ""))),
                }
                top15.append(item)
                if item["rank"] == 1:
                    summary["rank1"] = item
                elif item["rank"] == 3:
                    summary["rank3"] = item
                elif item["rank"] == 10:
                    summary["rank10"] = item
                if item["is_us"]:
                    summary["our_rank"] = item["rank"]
                    summary["our_score"] = sc
            summary["top15"] = top15
            our = summary["our_score"]
            if summary["rank1"]:
                summary["gap_to_1"] = round(summary["rank1"]["score"] - our, 5)
            if summary["rank3"]:
                summary["gap_to_3"] = round(summary["rank3"]["score"] - our, 5)
            if summary["rank10"]:
                summary["gap_to_10"] = round(summary["rank10"]["score"] - our, 5)
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    if ANOMALIES_FILE and os.path.exists(ANOMALIES_FILE):
        try:
            with open(ANOMALIES_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()[-6:]
            anoms = []
            for ln in reversed(lines):
                ln = ln.strip()
                if not ln:
                    continue
                obj = json.loads(ln)
                anoms.append({
                    "utc": obj.get("checked_utc"),
                    "team": obj.get("team"),
                    "type": obj.get("type"),
                    "new_rank": obj.get("new_rank"),
                    "new_score": obj.get("new_score"),
                    "delta_gm": obj.get("delta_gm"),
                })
            summary["anomalies"] = anoms
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    return summary


def build_state() -> dict:
    with open(MILESTONES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    now = datetime.datetime.now(datetime.timezone.utc)
    next_reset = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    reset_in_sec = int((next_reset - now).total_seconds())

    fleet = parse_status_telemetry(data.get("fleet", {}))
    radar = load_radar_summary(float(fleet.get("private_pb_score", 0.0)))

    sanitized_engines = []
    for eng in data.get("engines", []):
        e = dict(eng)
        hb_iso, hb_age = workspace_heartbeat(e.pop("workspace_paths", []))
        e["heartbeat_utc"] = hb_iso
        e["heartbeat_age_sec"] = hb_age
        sanitized_engines.append(e)

    return {
        "server_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reset_in_sec": reset_in_sec,
        "updated_utc": data.get("updated_utc"),
        "mission_title": data.get("mission_title", "MILESTONE WAR-ROOM"),
        "mission_subtitle": data.get("mission_subtitle", "Live Telemetry Dashboard"),
        "policy_badge": data.get("policy_badge", "PRIVATE TELEMETRY"),
        "fleet": fleet,
        "single_projection": data.get("single_projection", {}),
        "combo_projection": data.get("combo_projection", {}),
        "engines": sanitized_engines,
        "events": data.get("events", [])[:10],
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
            try:
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    html = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            except OSError:
                self._send_json(500, {"error": "index_missing"})
            return

        if path == "/api/state":
            if not self._is_authenticated(parsed):
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                state = build_state()
                self._send_json(200, state)
            except Exception as e:
                self._send_json(500, {"error": "state_error", "detail": str(e)})
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/api/auth":
            self._send_json(404, {"error": "not_found"})
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
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
    print(f"milestone-warroom listening on http://{HOST}:{PORT} (base={BASE_PATH or '/'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
