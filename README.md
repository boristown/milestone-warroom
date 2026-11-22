# Milestone War-Room

A minimalist, zero-dependency, dark-telemetry real-time war-room dashboard designed for high-stakes engineering sprints, algorithm competitions, and autonomous AI agent workflows.

## Highlights

- **Ultra-Minimalist Telemetry UI**: Dark obsidian aesthetic with crisp tabular numbers, 30-second radial auto-refresh countdown, and zero external CDN dependencies.
- **Data-Driven Animated SVG Topologies**:
  - `pipeline`: Multi-stage symmetric execution pipeline with flowing signal rails and luminous animated gradients on active bottlenecks.
  - `ceiling`: Resource/budget ceiling visualizer where an active bottleneck rises past the target ceiling line with animated compression chevrons and drops cleanly beneath the ceiling once completed.
- **Two-Level Collapsible Granularity**: Switch instantly between **Macro Phases (`大阶段`)**, **Focused Active Blocker (`聚焦攻坚`)**, and **Full Atomic Screws (`全螺丝钉`)**, or click any individual phase, milestone, or SVG topology block to expand/collapse.
- **Projected Score Trajectory**: Surfaces current score, benchmark targets, per-engine post-breakthrough projections, and combined multi-engine projections at a glance.
- **1-Line Atomic CLI (`scripts/update_milestone.py`)**: Safe for humans, CI jobs, or autonomous AI agents to check off milestones, auto-record elapsed time, and append broadcast events atomically (`tmp -> os.replace`).
- **Built-in Auth Gate**: Zero-dependency Python 3 stdlib server with magic-link (`?k=<TOKEN>`) cookie sign-in, passcode screen, `HttpOnly; Secure; SameSite=Lax` HMAC cookies, and `X-Robots-Tag: noindex`.

## Quick Start (30 seconds)

```bash
cp .env.example .env
# Edit WARROOM_TOKEN in .env (or export env vars directly)
WARROOM_TOKEN=demo-secret-key MILESTONES_FILE=milestones.example.json python3 server.py
```

Open:
- Magic-link auto-login: `http://127.0.0.1:13555/?k=demo-secret-key`
- Passcode screen: `http://127.0.0.1:13555/`

## Updating Milestones via CLI

```bash
# Mark a substep done and advance the next substep
python3 scripts/update_milestone.py --file milestones.json --id 2.3a --status done --note "Dependency graph complete"
python3 scripts/update_milestone.py --file milestones.json --id 2.3b --status active --note "Implementing zero-copy pass" \
  --event "Completed 2.3a, advancing to 2.3b" --event-tag "ENGINE 02" --event-level win

# Update projected score or fleet telemetry
python3 scripts/update_milestone.py --file milestones.json --engine-proj "engine1:98.4+|+4.2 pts|Projected #1"
python3 scripts/update_milestone.py --file milestones.json --cu 420.5 --burn 6.4 --quota 1/5
```

## Production Deployment

See [`deploy/warroom.service.example`](deploy/warroom.service.example) and [`deploy/nginx-subpath.conf.example`](deploy/nginx-subpath.conf.example) for a ~10 MB RAM `systemd` + `nginx` secret-subpath setup.

## License

MIT
