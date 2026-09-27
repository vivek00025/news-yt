"""The heartbeat of the app. A single background thread that, every TICK_SECONDS:
  1. makes sure every enabled client has today's videos planned (1-4, with upload times)
  2. starts generating any queued video whose retry time has arrived
  3. uploads any ready video whose scheduled time has arrived (auto-publish clients)
  4. gives up on anything that is now too late to publish usefully
  5. deletes old finished video/thumbnail files to save disk space
This is what makes the app "run daily and never stop": as long as the process is running,
new slots appear every day automatically with no cron job or manual step required.
"""
import shutil
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import daily
import db
import pipeline

_thread = None
_stop = threading.Event()
_dispatched_gen: set[int] = set()
_dispatched_pub: set[int] = set()
_lock = threading.Lock()


def _plan_all(log):
    for c in db.list_clients():
        if not c["enabled"]:
            continue
        try:
            daily.ensure_today_planned(c, log=log)
        except Exception as exc:  # noqa: BLE001 - one bad client must not stop planning for the rest
            log(f"Planning failed for {c['name']}: {exc}")


def _dispatch_generation(now_iso):
    busy_clients = {r["client_id"] for r in db.list_runs(limit=200)
                    if r["status"] in ("running", "publishing")}
    slots_left = config.MAX_PARALLEL_RUNS
    for r in db.queued_runs(now_iso):
        if slots_left <= 0:
            break
        with _lock:
            if r["id"] in _dispatched_gen:
                continue
        if r["client_id"] in busy_clients:
            continue
        with _lock:
            _dispatched_gen.add(r["id"])
        busy_clients.add(r["client_id"])
        slots_left -= 1
        pipeline.submit_generate(r["id"])


def _dispatch_publishing(now_iso):
    for r in db.ready_runs(now_iso):
        with _lock:
            if r["id"] in _dispatched_pub:
                continue
            _dispatched_pub.add(r["id"])
        pipeline.submit_publish(r["id"])


def _expire_late(log):
    """A queued-but-not-yet-generated video whose publish time is far in the past is more useful
    skipped than posted hours late with stale news."""
    cutoff_running = timedelta(hours=config.MAX_LATE_HOURS)
    for r in db.waiting_runs():
        if not r["publish_at"]:
            continue
        publish_at = datetime.fromisoformat(r["publish_at"])
        late = datetime.now(timezone.utc) - publish_at
        if r["status"] == "queued" and late > cutoff_running:
            db.update_run(r["id"], status="skipped", finished_at=db.now(),
                         error=f"Skipped: still not generated {late.seconds // 3600}h after its slot time.")
            log(f"{r['client_name']}: run #{r['id']} skipped, too late to generate.")


def _cleanup_old_files(log):
    cutoff = db.iso(datetime.now(timezone.utc) - timedelta(days=config.RETENTION_DAYS))
    for r in db.old_finished_runs(cutoff):
        for f in (r["final_path"], r["thumb_path"]):
            if f and Path(f).exists():
                try:
                    Path(f).unlink()
                except OSError:
                    pass
        run_dir = config.RUNS_DIR / str(r["id"])
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)


def _forget_finished():
    with _lock:
        active = {r["id"] for r in db.list_runs(limit=500) if r["status"] in ("queued", "running", "publishing")}
        _dispatched_gen.intersection_update(active)
        _dispatched_pub.intersection_update(active)


def _tick():
    log = print
    now_iso = db.now()
    try:
        _plan_all(log)
        _dispatch_generation(now_iso)
        _dispatch_publishing(now_iso)
        _expire_late(log)
        _cleanup_old_files(log)
        _forget_finished()
    except Exception:  # noqa: BLE001 - the ticker itself must never die
        traceback.print_exc()


def _loop():
    while not _stop.is_set():
        _tick()
        _stop.wait(config.TICK_SECONDS)


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="ticker", daemon=True)
    _thread.start()


def is_alive() -> bool:
    return bool(_thread and _thread.is_alive())
