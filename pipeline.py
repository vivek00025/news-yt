"""News -> Groq script -> Manus (video + thumbnail) -> ffmpeg (footer + outro) -> YouTube.

Two independent stages, so a video can finish well before its upload time:
  generate_run(run_id): queued -> running -> ready (or failed, requeued for retry)
  publish_run(run_id):  ready  -> publishing -> done (or failed, requeued for retry)
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import crypto
import db
from services import groq_script, news, video_edit, youtube
from services.manus_client import ManusClient, ManusError, ManusTaskFailed

executor = ThreadPoolExecutor(max_workers=config.MAX_PARALLEL_RUNS, thread_name_prefix="gen")
publish_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pub")

RETRY_BACKOFF_MIN = (2, 10, 30)   # minutes before attempt 2, 3, 4...


class PipelineError(RuntimeError):
    """Not worth retrying (bad config, no news, etc). Fails the run immediately."""


def _log(run_id):
    return lambda m: db.append_log(run_id, m)


def _backoff(attempts: int) -> int:
    return RETRY_BACKOFF_MIN[min(attempts - 1, len(RETRY_BACKOFF_MIN) - 1)]


def _fail_generate(run_id, prior_attempts, exc, permanent=False):
    attempts = int(prior_attempts) + 1  # ✅ Convert to int first
    db.append_log(run_id, f"FAILED: {exc}")
    if permanent or attempts >= config.MAX_ATTEMPTS:  # ✅ Now comparing int >= int
        db.update_run(run_id, status="failed", error=str(exc)[:1000], finished_at=db.now(), attempts=attempts)
        db.append_log(run_id, f"Giving up after {attempts} attempt(s).")
        return
    delay = _backoff(attempts)
    retry_at = db.iso(datetime.now(timezone.utc) + timedelta(minutes=delay))
    db.update_run(run_id, status="queued", error=str(exc)[:1000], attempts=attempts, retry_after=retry_at,
                  step=f"Retry {attempts + 1}/{config.MAX_ATTEMPTS} scheduled")
    db.append_log(run_id, f"Will retry (attempt {attempts + 1}/{config.MAX_ATTEMPTS}) after {delay} min.")


def _fail_publish(run_id, prior_attempts, exc, permanent=False):
    attempts = prior_attempts + 1
    db.append_log(run_id, f"FAILED: {exc}")
    if permanent or attempts >= config.MAX_ATTEMPTS:
        db.update_run(run_id, status="failed", error=str(exc)[:1000], finished_at=db.now(), pub_attempts=attempts)
        db.append_log(run_id, f"Giving up on uploading after {attempts} attempt(s). The video file is kept.")
        return
    delay = _backoff(attempts)
    retry_at = db.iso(datetime.now(timezone.utc) + timedelta(minutes=delay))
    db.update_run(run_id, status="ready", error=str(exc)[:1000], pub_attempts=attempts, retry_after=retry_at)
    db.append_log(run_id, f"Will retry the upload (attempt {attempts + 1}/{config.MAX_ATTEMPTS}) after {delay} min.")


# ------------------------------------------------------------ stage 1: generate
def submit_generate(run_id: int):
    executor.submit(generate_run, run_id)


def generate_run(run_id: int):
    run = db.get_run(run_id)
    client = db.get_client(run["client_id"])
    log = _log(run_id)

    def step(name):
        db.update_run(run_id, step=name)
        log(f"== {name}")

    if not client or not client["enabled"]:
        db.update_run(run_id, status="skipped", finished_at=db.now(), error="Client disabled")
        return
    db.update_run(run_id, status="running", retry_after=None)
    try:
        log(f"Client: {client['name']} | {client['duration']}s | {client['aspect']}"
            + (f" | video {run['slot']} of {client['videos_per_day']} for {run['plan_date']}" if run["slot"] else " | manual run"))

        manus_key = crypto.decrypt(client["manus_api_key_enc"])
        if not manus_key:
            raise PipelineError("No Manus API key saved for this client.")
        if not config.GROQ_API_KEY:
            raise PipelineError("GROQ_API_KEY is not set in .env.")
        if client["auto_publish"] and not client["yt_refresh_enc"]:
            raise PipelineError("Auto-publish is on but YouTube is not connected for this client.")
        video_edit.check_ffmpeg()

        workdir = config.RUNS_DIR / str(run_id)
        workdir.mkdir(parents=True, exist_ok=True)

        step("Fetching news")
        articles = news.fetch_headlines(client, db.used_hashes(client["id"]), log=log)
        for i, a in enumerate(articles):
            log(f"  [{i}] {a['title']} ({a['source']})")

        step("Writing script and SEO metadata with Groq")
        avoid = db.recent_titles(client["id"])
        script = groq_script.generate_script(client, articles, avoid_topics=avoid, log=log)
        used = [articles[i] for i in script["used_headline_indexes"]]
        script["manus_prompt"] = groq_script.build_manus_prompt(client, script)
        script["sources"] = [{"source": a["source"], "title": a["title"]} for a in used]
        db.update_run(run_id, script_json=json.dumps(script, ensure_ascii=False, indent=2))
        log(f"Title: {script['title']}  ({script['voiceover_words']} words, {len(script['tags'])} tags)")

        step("Generating video and thumbnail with Manus")
        manus = ManusClient(manus_key)
        try:
            task = manus.create_task(script["manus_prompt"], title=script["title"], profile=client["manus_profile"])
            db.update_run(run_id, manus_task_id=task["task_id"], manus_task_url=task.get("task_url"))
            log(f"Manus task {task['task_id']} created. Waiting (this can take several minutes)...")
            video_att, thumb_att = manus.wait_for_outputs(task["task_id"], log=log)
        except ManusTaskFailed as exc:
            raise PipelineError(f"Manus task failed: {exc}") from exc

        step("Downloading files")
        raw = manus.download(video_att["url"], workdir / "raw.mp4")
        info = video_edit.probe(raw)
        log(f"Video: {info['width']}x{info['height']}, {info['duration']:.1f}s, "
            f"audio={'yes' if info['has_audio'] else 'no'}")
        if abs(info["duration"] - int(client["duration"])) > max(3, int(client["duration"]) * 0.25):
            log(f"Warning: length differs noticeably from the requested {client['duration']}s.")

        thumb_src, thumb_status = None, "manus"
        if thumb_att:
            try:
                thumb_src = manus.download(thumb_att["url"], workdir / "thumb_raw")
            except ManusError as exc:
                log(f"Thumbnail download failed, will use a video frame instead: {exc}")
        if not thumb_src:
            thumb_status = "frame"

        step("Adding footer and outro")
        footer, outro = db.resolve_asset(client["id"], "footer"), db.resolve_asset(client["id"], "outro")
        log(f"Footer: {footer['filename'] if footer else 'none'} | Outro: {outro['filename'] if outro else 'none'}")
        final = video_edit.compose(
            raw, workdir / "final.mp4", client["aspect"],
            Path(footer["path"]) if footer else None,
            Path(outro["path"]) if outro else None, workdir / "tmp")

        thumb_dst = workdir / "thumbnail.jpg"
        if thumb_src:
            video_edit.make_thumbnail(thumb_src, thumb_dst)
        else:
            video_edit.frame_thumbnail(final, thumb_dst)
        db.update_run(run_id, final_path=str(final), thumb_path=str(thumb_dst), thumb_status=thumb_status)
        db.mark_used(client["id"], used)
        log(f"Final video ready ({final.stat().st_size / 1e6:.1f} MB). Thumbnail: {thumb_status}.")

        db.update_run(run_id, status="ready", step="Ready", finished_at=db.now())
        log("Ready." + (" Waiting for its scheduled upload time." if client["auto_publish"] else " Awaiting manual approval."))
    except PipelineError as exc:
        _fail_generate(run_id, run["attempts"], exc, permanent=True)
    except Exception as exc:  # noqa: BLE001 - network/Manus/ffmpeg hiccups are retried
        _fail_generate(run_id, run["attempts"], exc, permanent=False)


# ------------------------------------------------------------ stage 2: publish
def submit_publish(run_id: int):
    db.update_run(run_id, status="publishing", retry_after=None)
    publish_executor.submit(publish_run, run_id)


def _description(script, aspect, footer_text=""):
    src = "\n".join(f"- {a['source'] or 'Source'}: {a['title']}" for a in script.get("sources", []) if a.get("title"))
    hashtags = " ".join(script.get("hashtags", []))
    text = script["description"].strip()
    if src:
        text += f"\n\nSources reported:\n{src}"
    if footer_text.strip():
        text += f"\n\n{footer_text.strip()}"
    text += "\n\nAI-generated video."
    if aspect == "9:16":
        text += " #Shorts"
    if hashtags:
        text += f"\n{hashtags}"
    return text


def publish_run(run_id: int):
    run = db.get_run(run_id)
    client = db.get_client(run["client_id"])
    log = _log(run_id)
    if not client or not client["enabled"]:
        db.update_run(run_id, status="ready" if run["final_path"] else "failed")
        return
    try:
        if not run["final_path"] or not Path(run["final_path"]).exists():
            raise PipelineError("The final video file is missing.")
        if not client["yt_refresh_enc"]:
            raise PipelineError("YouTube is not connected for this client.")
        script = json.loads(run["script_json"])
        log(f"== Uploading to YouTube ({client['yt_channel_title'] or 'channel'}, {client['privacy']})")
        tags = script.get("tags", []) + [t.strip() for t in client["default_tags"].split(",") if t.strip()]
        thumb = Path(run["thumb_path"]) if run["thumb_path"] and Path(run["thumb_path"]).exists() else None
        vid, thumb_note = youtube.upload(client, Path(run["final_path"]), script["title"],
                                         _description(script, client["aspect"], client["description_footer"]),
                                         tags, thumbnail=thumb, log=log)
        db.update_run(run_id, youtube_id=vid, status="done", step="Published", finished_at=db.now())
        log(f"Published: https://youtu.be/{vid} ({thumb_note})")
        if client["yt_status"]:
            db.update_client(client["id"], {"yt_status": ""})
    except PipelineError as exc:
        _fail_publish(run_id, run["pub_attempts"], exc, permanent=True)
    except youtube.YouTubeError as exc:
        if exc.reauth:
            db.update_client(client["id"], {"yt_status": "reauth"})
        _fail_publish(run_id, run["pub_attempts"], exc, permanent=exc.permanent)
    except Exception as exc:  # noqa: BLE001
        _fail_publish(run_id, run["pub_attempts"], exc, permanent=False)


def is_busy(client_id: int) -> bool:
    return any(r["client_id"] == client_id and r["status"] in ("running", "publishing")
              for r in db.list_runs(limit=200))


# ------------------------------------------------------------ manual triggers (dashboard buttons)
def submit_manual_run(client_id: int) -> int | None:
    """'Run now': one extra video outside the daily plan, generated immediately."""
    if db.has_open_manual(client_id):
        return None
    run_id = db.create_run(client_id, trigger="manual")
    submit_generate(run_id)
    return run_id


def submit_manual_publish(run_id: int) -> bool:
    run = db.get_run(run_id)
    if not run or run["status"] not in ("ready", "failed"):
        return False
    submit_publish(run_id)
    return True
