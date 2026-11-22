#!/usr/bin/env python3
"""Atomic CLI helper to update milestones.json safely from scripts or AI agents."""
from __future__ import annotations
import argparse
import datetime
import json
import os
import sys

DEFAULT_PATH = os.environ.get("MILESTONES_FILE", "milestones.json")
VALID_STATUS = {"done", "active", "blocked", "queued"}


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def update_node(node: dict, status: str | None, note: str | None, metric: str | None, now: str) -> None:
    if status:
        prev = node.get("status")
        node["status"] = status
        if status == "active":
            if prev != "active" or not node.get("started_utc"):
                node["started_utc"] = now
            node["completed_utc"] = None
        elif status == "done":
            if not node.get("started_utc"):
                node["started_utc"] = now
            node["completed_utc"] = now
        elif status == "queued":
            node["completed_utc"] = None
    if note is not None:
        node["note"] = note
    if metric is not None:
        node["metric"] = metric


def iter_milestones(eng: dict):
    for ph in eng.get("phases", []) or []:
        for ms in ph.get("milestones", []) or []:
            yield ph, ms


def find_and_update(data: dict, target_id: str, status: str | None, note: str | None, metric: str | None, now: str) -> bool:
    for eng in data.get("engines", []):
        for ph, ms in iter_milestones(eng):
            if ph.get("id") == target_id:
                if metric is not None:
                    ph["summary_metric"] = metric
                if note is not None:
                    ph["projected_unlock"] = note
                return True
            if ms.get("id") == target_id:
                update_node(ms, status, note, metric, now)
                if status == "active":
                    eng["current_focus"] = target_id
                return True
            for sub in ms.get("substeps", []) or []:
                if sub.get("id") == target_id:
                    update_node(sub, status, note, metric, now)
                    if status == "active":
                        eng["current_focus"] = target_id
                        if ms.get("status") == "queued":
                            update_node(ms, "active", None, None, now)
                    elif status == "done":
                        subs = ms.get("substeps", [])
                        if subs and all(s.get("status") == "done" for s in subs):
                            update_node(ms, "done", None, None, now)
                    return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Update Milestone War-Room JSON atomically")
    ap.add_argument("--file", default=DEFAULT_PATH)
    ap.add_argument("--id", help="Phase, milestone, or substep ID (e.g. 1.3b, 2.3a, P2-2)")
    ap.add_argument("--status", choices=sorted(VALID_STATUS))
    ap.add_argument("--note", help="Updated note (or projected_unlock on phase ID)")
    ap.add_argument("--metric", help="Updated metric pill (or summary_metric on phase ID)")
    ap.add_argument("--engine-proj", help="Update engine projected score as engineId:score|delta|rank")
    ap.add_argument("--focus", help="Set engine focus as engineId:stepId")
    ap.add_argument("--bonus", help="Update engine bonus note as engineId:text")
    ap.add_argument("--event", help="Prepend a broadcast event message")
    ap.add_argument("--event-tag", default="COMMANDER")
    ap.add_argument("--event-level", choices=["win", "info", "alert"], default="info")
    ap.add_argument("--cu", type=float)
    ap.add_argument("--burn", type=float)
    ap.add_argument("--boxes", type=int)
    ap.add_argument("--quota", help="used/total e.g. 0/5")
    ap.add_argument("--quota-day", help="YYYY-MM-DD UTC")
    ap.add_argument("--pb", type=float)
    ap.add_argument("--pb-ver")
    ap.add_argument("--pb-ref")
    args = ap.parse_args()

    with open(args.file, "r", encoding="utf-8") as f:
        data = json.load(f)

    now = utc_now()
    changed = False

    if args.id:
        if not find_and_update(data, args.id, args.status, args.note, args.metric, now):
            print(f"ERROR: ID '{args.id}' not found", file=sys.stderr)
            return 2
        changed = True

    if args.engine_proj and ":" in args.engine_proj:
        eng_id, payload = args.engine_proj.split(":", 1)
        parts = payload.split("|")
        for eng in data.get("engines", []):
            if eng.get("id") == eng_id:
                if len(parts) >= 1 and parts[0]:
                    eng["projected_score"] = parts[0]
                if len(parts) >= 2 and parts[1]:
                    eng["projected_delta"] = parts[1]
                if len(parts) >= 3 and parts[2]:
                    eng["projected_rank"] = parts[2]
                changed = True

    if args.focus and ":" in args.focus:
        eng_id, step_id = args.focus.split(":", 1)
        for eng in data.get("engines", []):
            if eng.get("id") == eng_id:
                eng["current_focus"] = step_id
                changed = True

    if args.bonus and ":" in args.bonus:
        eng_id, btext = args.bonus.split(":", 1)
        for eng in data.get("engines", []):
            if eng.get("id") == eng_id:
                eng["bonus_note"] = btext
                changed = True

    fleet = data.setdefault("fleet", {})
    if args.cu is not None:
        fleet["cu_balance"] = round(args.cu, 2)
        changed = True
    if args.burn is not None:
        fleet["cu_burn_per_hr"] = round(args.burn, 2)
        changed = True
    if args.boxes is not None:
        fleet["active_boxes"] = args.boxes
        changed = True
    if args.quota and "/" in args.quota:
        u, t = args.quota.split("/", 1)
        fleet["quota_used"] = int(u)
        fleet["quota_total"] = int(t)
        changed = True
    if args.quota_day:
        fleet["quota_day_utc"] = args.quota_day
        changed = True
    if args.pb is not None:
        fleet["private_pb_score"] = args.pb
        changed = True
    if args.pb_ver:
        fleet["private_pb_version"] = args.pb_ver
        changed = True
    if args.pb_ref:
        fleet["private_pb_ref"] = args.pb_ref
        changed = True

    if args.event:
        events = data.setdefault("events", [])
        events.insert(0, {
            "utc": now,
            "tag": args.event_tag,
            "level": args.event_level,
            "text": args.event,
        })
        data["events"] = events[:20]
        changed = True

    if changed:
        data["updated_utc"] = now
        tmp = args.file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, args.file)
        print(f"OK updated {args.file} @ {now}")
    else:
        print("No changes specified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
