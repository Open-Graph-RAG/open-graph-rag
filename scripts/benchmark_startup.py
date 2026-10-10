#!/usr/bin/env python3
"""Benchmark stack startup time driven by scripts/restart_stack.sh.

Measures the wall-clock time from "scripts/restart_stack.sh --yes" invocation
to when every service defined in the base compose.yaml reaches a ready state.

A service is considered ready when:
  - it has a healthcheck and Health == "healthy"
  - it has no healthcheck and State == "running"
  - AND (for the "fully_ready" metric) any readiness probe we know about
    returns a 2xx status.  This catches the case where the container is
    "running" but the app inside has not finished booting (e.g. librechat).

Usage:
  python3 scripts/benchmark_startup.py --label baseline --smoke
  python3 scripts/benchmark_startup.py --label optimized --no-build --smoke
  python3 scripts/benchmark_startup.py --label final --smoke --rounds 3

NOTE on `--smoke`: the mcp_bridge_smoke probe runs `scripts/smoke.py` inside
the mcp container, which (per AGENTS.md) "may invoke provider APIs" and
therefore can incur paid usage.  Skip --smoke if you do not want that.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "compose.yaml"
RESTART_SCRIPT = REPO_ROOT / "scripts" / "restart_stack.sh"
PROJECT_NAME = "librechat-lightrag"

# Services defined in the base compose.yaml. n8n has no healthcheck; we treat
# "running" as its ready signal to keep the benchmark aligned with the script.
BASE_SERVICES = ["ollama", "mongodb", "postgres", "lightrag", "mcp", "n8n", "librechat"]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def active_compose_files() -> list[str]:
    """Return the compose files the live project was started with (relative to
    REPO_ROOT). Falls back to compose.yaml when no project is registered."""
    r = run(["docker", "compose", "ls", "--format", "json"])
    try:
        entries = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        entries = []
    for entry in entries:
        if entry.get("Name") != PROJECT_NAME:
            continue
        cf = entry.get("ConfigFiles")
        rels: list[str] = []
        items = cf.split(",") if isinstance(cf, str) else (cf or [])
        for p in items:
            p = p.strip()
            if not p:
                continue
            try:
                rels.append(str(Path(p).resolve().relative_to(REPO_ROOT)))
            except ValueError:
                rels.append(p)
        return rels or ["compose.yaml"]
    return ["compose.yaml"]


def compose_cmd_args(files: list[str], *extra: str) -> list[str]:
    args: list[str] = ["docker", "compose"]
    for f in files:
        args.extend(["-f", str(REPO_ROOT / f)])
    args.extend(extra)
    return args


def ps_rows(files: list[str]) -> list[dict]:
    r = run(compose_cmd_args(files, "ps", "--format", "json"))
    rows: list[dict] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def is_container_ready(row: dict) -> bool:
    """True if compose reports the container as ready (running + healthy when
    a healthcheck is defined)."""
    state = (row.get("State") or "").lower()
    health = (row.get("Health") or "").lower()
    status = (row.get("Status") or "").lower()
    if state != "running":
        return False
    if "(no healthcheck)" in status or health in ("", "none"):
        return True
    return health == "healthy" or "(healthy)" in status


def probe_ready(svc: str, files: list[str]) -> bool:
    """Hit an HTTP /health endpoint through `docker compose exec -T`, so the
    benchmark stays correct even if the project name or container_name
    directive changes."""
    port_for = {"librechat": 3080, "mcp": 8000, "lightrag": 9621}
    port = port_for.get(svc)
    if port is None:
        return True
    code = run(compose_cmd_args(files, "exec", "-T", svc, "python", "-c",
                                f"import urllib.request; "
                                f"print(urllib.request.urlopen('http://127.0.0.1:{port}/health', timeout=5).status)"),
               timeout=15).stdout.strip()
    return code.isdigit() and 200 <= int(code) < 400


def poll_until_ready(
    services: list[str],
    timeout: float,
    t0: float,
    files: list[str],
) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, str], str]:
    """Poll compose + readiness probes until all services are fully ready
    (container healthy AND any probe succeeds)."""
    container_ready_at: dict[str, float] = {}
    first_seen: dict[str, float] = {}
    last_state: dict[str, str] = {}
    fully_ready_at: dict[str, float] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = ps_rows(files)
        by_svc = {(row.get("Service") or "").lower(): row for row in rows if row.get("Service")}
        now = time.monotonic()
        for svc in services:
            row = by_svc.get(svc)
            if row is None:
                continue
            if svc not in first_seen:
                first_seen[svc] = now
            last_state[svc] = (
                f"state={row.get('State', '')} "
                f"health={row.get('Health', '')} "
                f"status={row.get('Status', '')}"
            )
            if svc not in container_ready_at and is_container_ready(row):
                container_ready_at[svc] = now
            if svc not in fully_ready_at and svc in container_ready_at and probe_ready(svc, files):
                fully_ready_at[svc] = now
        if len(fully_ready_at) == len(services):
            return container_ready_at, first_seen, fully_ready_at, last_state, "ok"
        time.sleep(0.5)
    return container_ready_at, first_seen, fully_ready_at, last_state, "timeout"


def run_one(label: str, restart_args: list[str], timeout: float) -> dict:
    print(f"\n=== Run: {label} ===", flush=True)
    cmd = ["bash", str(RESTART_SCRIPT), "--yes", *restart_args]
    print(f"  $ {' '.join(cmd)}", flush=True)
    files = active_compose_files()
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError as e:
        return {"label": label, "error": f"cannot launch script: {e}", "elapsed": 0.0}
    captured_lines: list[str] = []
    # Read the script's stdout while it runs, with a hard watchdog on the
    # remaining timeout so a stuck restart_stack.sh cannot wedge the run.
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            captured_lines.append(line)
            if time.monotonic() - t0 > timeout:
                proc.kill()
                proc.wait(timeout=10)
                return {
                    "label": label,
                    "error": f"script exceeded timeout ({timeout:.0f}s); killed",
                    "elapsed": time.monotonic() - t0,
                }
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    t_post = time.monotonic()
    print(f"  restart_stack.sh exited in {t_post - t0:.2f}s (code {proc.returncode})", flush=True)
    if proc.returncode != 0:
        return {"label": label, "error": f"exit {proc.returncode}", "elapsed": t_post - t0}

    container_ready_at, first_seen, fully_ready_at, last_state, outcome = poll_until_ready(
        BASE_SERVICES, timeout - (t_post - t0), t0, files,
    )
    t_end = time.monotonic()
    total = t_end - t0
    print(f"  outcome: {outcome}", flush=True)
    for svc in BASE_SERVICES:
        cr = container_ready_at.get(svc)
        fr = fully_ready_at.get(svc)
        fs = first_seen.get(svc)
        if cr is not None:
            fs_rel = (fs - t0) if fs is not None else 0.0
            fr_rel = (fr - t0) if fr is not None else 0.0
            print(f"    {svc:12s}: first_seen {fs_rel:7.2f}s  "
                  f"container_ready {(cr - t0):7.2f}s  fully_ready {fr_rel:7.2f}s")
        else:
            print(f"    {svc:12s}: NOT READY  last={last_state.get(svc, 'absent')}")
    print(f"  total wall clock: {total:.2f}s", flush=True)
    print("  --- last script output lines ---")
    for line in captured_lines[-5:]:
        print(f"  | {line.rstrip()}")
    return {
        "label": label,
        "exit_code": proc.returncode,
        "restart_script_seconds": t_post - t0,
        "fully_ready_seconds": total,
        "outcome": outcome,
        "per_service": {
            svc: {
                "first_seen_s": (first_seen[svc] - t0) if first_seen.get(svc) is not None else None,
                "container_ready_s": (container_ready_at[svc] - t0) if container_ready_at.get(svc) is not None else None,
                "fully_ready_s": (fully_ready_at[svc] - t0) if fully_ready_at.get(svc) is not None else None,
                "last_state": last_state.get(svc),
            }
            for svc in BASE_SERVICES
        },
    }


def smoke_check(files: list[str]) -> dict:
    out: dict = {}
    checks = [
        ("mcp_health", compose_cmd_args(files, "exec", "-T", "mcp", "python", "-c",
                                       "import urllib.request; "
                                       "print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status)")),
        ("lightrag_health", compose_cmd_args(files, "exec", "-T", "lightrag", "python", "-c",
                                            "import urllib.request; "
                                            "print(urllib.request.urlopen('http://127.0.0.1:9621/health', timeout=5).status)")),
        ("librechat_root", ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                            "http://127.0.0.1:3080/"]),
        # NOTE: smoke.py may invoke provider APIs per AGENTS.md
        ("mcp_bridge_smoke", compose_cmd_args(files, "exec", "-T", "mcp",
                                              "python", "smoke.py")),
    ]
    for name, cmd in checks:
        try:
            r = run(cmd, timeout=60)
            out[name] = {"stdout": r.stdout.strip()[:200], "stderr": r.stderr.strip()[:200], "code": r.returncode}
        except subprocess.TimeoutExpired:
            out[name] = {"stdout": "", "stderr": "timeout", "code": -1}
    return out


def summarize(results: list[dict]) -> dict:
    totals = [r["fully_ready_seconds"] for r in results if r.get("fully_ready_seconds")]
    if not totals:
        return {}
    return {
        "min": min(totals),
        "max": max(totals),
        "median": statistics.median(totals),
        "mean": statistics.mean(totals),
        "n": len(totals),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--label", default="run")
    p.add_argument("--restart-args", default="",
                   help="Extra args forwarded to restart_stack.sh (string, shell-split).")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--smoke", action="store_true",
                   help="Run post-startup probes (note: mcp smoke may call provider APIs).")
    p.add_argument("--rounds", type=int, default=1)
    args = p.parse_args()

    extra = [a for a in args.restart_args.split() if a]
    results: list[dict] = []
    for i in range(args.rounds):
        label = args.label if args.rounds == 1 else f"{args.label}_{i + 1}"
        results.append(run_one(label, extra, args.timeout))
    smoke = smoke_check(active_compose_files()) if args.smoke else None
    summary = summarize(results)
    out_file = REPO_ROOT / "scripts" / f"benchmark_{args.label}_{int(time.time())}.json"
    payload = {
        "args": vars(args),
        "summary": summary,
        "results": results,
        "smoke": smoke,
    }
    out_file.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nResults written to {out_file}")
    if summary:
        print(f"Summary fully_ready_seconds: median={summary['median']:.2f}  "
              f"min={summary['min']:.2f}  max={summary['max']:.2f}  n={summary['n']}")
    if smoke:
        print("Smoke check:")
        print(json.dumps(smoke, indent=2))


if __name__ == "__main__":
    main()