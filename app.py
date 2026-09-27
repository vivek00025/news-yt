import hmac
import json
import secrets
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (Flask, Response, abort, flash, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.utils import secure_filename

import config
import crypto
import daily
import db
import pipeline
import ticker
from services import youtube

app = Flask(__name__)
app.secret_key = crypto.flask_secret()
app.config["MAX_CONTENT_LENGTH"] = 600 * 1024 * 1024  # outro videos can be large

FOOTER_EXT = {".png", ".jpg", ".jpeg", ".webp"}
OUTRO_EXT = {".mp4", ".mov", ".webm", ".m4v", ".png", ".jpg", ".jpeg"}
TIMEZONES = ["UTC", "Asia/Kolkata", "Asia/Karachi", "Asia/Dubai", "Europe/London", "Europe/Berlin",
             "America/New_York", "America/Chicago", "America/Los_Angeles", "Australia/Sydney"]
DEFAULT_SLOTS_BY_COUNT = {1: "18:00", 2: "09:00,18:00", 3: "09:00,13:00,18:00", 4: "09:00,13:00,18:00,21:00"}


# ---------------------------------------------------------------- security
@app.before_request
def guard():
    if request.endpoint == "static":
        return None
    if config.ADMIN_PASSWORD:
        a = request.authorization
        ok = a and hmac.compare_digest(a.username or "", config.ADMIN_USER) and \
            hmac.compare_digest(a.password or "", config.ADMIN_PASSWORD)
        if not ok:
            return Response("Login required.", 401, {"WWW-Authenticate": 'Basic realm="Newsreel"'})
    if request.method == "POST":
        token = request.form.get("csrf_token", "")
        if not token or not hmac.compare_digest(token, session.get("csrf", "")):
            abort(400, "Invalid or missing CSRF token. Reload the page and try again.")
    return None


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return {"csrf_token": session["csrf"], "yt_ready": youtube.is_configured(),
            "auth_on": bool(config.ADMIN_PASSWORD), "ticker_alive": ticker.is_alive(),
            "config_tick": config.TICK_SECONDS, "config_min": config.DURATION_MIN}


@app.template_filter("when")
def when_filter(iso):
    return iso.replace("T", " ").replace("+00:00", " UTC") if iso else ""


@app.template_filter("localtime")
def localtime_filter(iso, tzname):
    if not iso:
        return ""
    try:
        dt = datetime_fromiso(iso).astimezone(ZoneInfo(tzname or "UTC"))
        return dt.strftime("%d %b, %H:%M")
    except Exception:  # noqa: BLE001
        return iso


def datetime_fromiso(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso)


# ---------------------------------------------------------------- helpers
def clean_client_form(form, existing=None):
    errors, d = [], {}
    d["name"] = form.get("name", "").strip()
    if not d["name"]:
        errors.append("Client name is required.")
    d["enabled"] = 1 if form.get("enabled") else 0
    d["country"] = (form.get("country", "US").strip().upper() or "US")[:2]
    d["language"] = (form.get("language", "en").strip().lower() or "en")[:5]
    d["news_query"] = form.get("news_query", "").strip() or "politics"
    d["extra_feeds"] = "\n".join(u.strip() for u in form.get("extra_feeds", "").splitlines()
                                 if u.strip().startswith("http"))
    try:
        d["headlines_count"] = min(15, max(3, int(form.get("headlines_count", 8))))
    except ValueError:
        d["headlines_count"] = 8
    d["script_language"] = form.get("script_language", "").strip() or "English"
    d["style_notes"] = form.get("style_notes", "").strip()[:1000]

    dur_choice = form.get("duration_choice", "20")
    if dur_choice == "custom":
        try:
            d["duration"] = int(form.get("duration_custom", 0))
        except ValueError:
            d["duration"] = 0
    else:
        try:
            d["duration"] = int(dur_choice)
        except ValueError:
            d["duration"] = 0
    if not (config.DURATION_MIN <= d["duration"] <= config.DURATION_MAX):
        errors.append(f"Duration must be between {config.DURATION_MIN} and {config.DURATION_MAX} seconds.")

    d["aspect"] = form.get("aspect", "9:16")
    if d["aspect"] not in config.FRAME_SIZES:
        errors.append("Aspect ratio must be 9:16 or 16:9.")

    key = form.get("manus_api_key", "").strip()
    if key:
        d["manus_api_key_enc"] = crypto.encrypt(key)
    elif existing is None:
        d["manus_api_key_enc"] = ""
    d["manus_profile"] = form.get("manus_profile", "standard")
    if d["manus_profile"] not in ("standard", "lite", "max"):
        d["manus_profile"] = "standard"

    try:
        d["videos_per_day"] = max(1, min(config.MAX_VIDEOS_PER_DAY, int(form.get("videos_per_day", 1))))
    except ValueError:
        d["videos_per_day"] = 1
    raw_slots = form.get("slot_times", "").strip() or DEFAULT_SLOTS_BY_COUNT[d["videos_per_day"]]
    try:
        times = daily.parse_slot_times(raw_slots)
        if len(times) < d["videos_per_day"]:
            errors.append(f"Give at least {d['videos_per_day']} upload time(s), one per video a day.")
        d["slot_times"] = ",".join(times)
    except ValueError as exc:
        errors.append(str(exc))
        d["slot_times"] = raw_slots

    d["timezone"] = form.get("timezone", "UTC").strip() or "UTC"
    try:
        ZoneInfo(d["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"Unknown timezone '{d['timezone']}'.")

    d["privacy"] = form.get("privacy", "private")
    if d["privacy"] not in ("public", "unlisted", "private"):
        d["privacy"] = "private"
    d["auto_publish"] = 1 if form.get("auto_publish") else 0
    d["category_id"] = "".join(ch for ch in form.get("category_id", "25") if ch.isdigit()) or "25"
    d["default_tags"] = form.get("default_tags", "").strip()[:300]
    d["description_footer"] = form.get("description_footer", "").strip()[:500]
    d["ai_disclosure"] = 1 if form.get("ai_disclosure") else 0
    return d, errors


def save_asset(client_id, kind, file):
    allowed = FOOTER_EXT if kind == "footer" else OUTRO_EXT
    name = secure_filename(file.filename or "")
    ext = Path(name).suffix.lower()
    if not name or ext not in allowed:
        raise ValueError(f"{kind.title()} must be one of: {', '.join(sorted(allowed))}")
    folder = config.ASSETS_DIR / (f"client_{client_id}" if client_id else "default")
    folder.mkdir(parents=True, exist_ok=True)
    old = db.delete_asset(client_id, kind)
    if old:
        Path(old).unlink(missing_ok=True)
    dest = folder / f"{kind}{ext}"
    file.save(dest)
    db.set_asset(client_id, kind, name, dest)


def client_or_404(client_id):
    c = db.get_client(client_id)
    if not c:
        abort(404)
    return c


# ---------------------------------------------------------------- dashboard
@app.get("/")
def dashboard():
    clients = db.list_clients()
    plans = {c["id"]: db.runs_for_plan(c["id"], daily.today_str(c)) for c in clients}
    return render_template("dashboard.html", clients=clients, plans=plans,
                           runs=db.list_runs(limit=15), busy=pipeline)


# ---------------------------------------------------------------- clients
@app.route("/clients/new", methods=["GET", "POST"])
def client_new():
    if request.method == "POST":
        data, errors = clean_client_form(request.form)
        if not errors:
            cid = db.create_client(data)
            flash("Client created. Now upload branding assets, add a Manus key and connect YouTube.", "ok")
            return redirect(url_for("client_detail", client_id=cid))
        for e in errors:
            flash(e, "err")
        return render_template("client.html", client=data, is_new=True, timezones=TIMEZONES, runs=[],
                              slots=[], default_slots=DEFAULT_SLOTS_BY_COUNT)
    defaults = {"name": "", "enabled": 1, "country": "US", "language": "en", "news_query": "politics",
                "extra_feeds": "", "headlines_count": 8, "script_language": "English", "style_notes": "",
                "duration": 20, "aspect": "9:16", "manus_profile": "standard", "videos_per_day": 1,
                "slot_times": "18:00", "timezone": "UTC", "privacy": "private", "auto_publish": 1,
                "category_id": "25", "default_tags": "", "description_footer": "", "ai_disclosure": 1,
                "manus_api_key_enc": "", "google_secrets_enc": ""}
    return render_template("client.html", client=defaults, is_new=True, timezones=TIMEZONES, runs=[],
                          slots=[], default_slots=DEFAULT_SLOTS_BY_COUNT)


@app.get("/clients/<int:client_id>")
def client_detail(client_id):
    c = client_or_404(client_id)
    assets = {k: db.get_asset(client_id, k) for k in ("footer", "outro")}
    defaults = {k: db.get_asset(None, k) for k in ("footer", "outro")}
    plan = db.runs_for_plan(client_id, daily.today_str(c))
    slots = daily.slots_for(c)
    return render_template("client.html", client=c, is_new=False, timezones=TIMEZONES,
                           assets=assets, defaults=defaults, runs=db.list_runs(client_id, 12),
                           plan=plan, slots=slots, default_slots=DEFAULT_SLOTS_BY_COUNT)


@app.post("/clients/<int:client_id>")
def client_update(client_id):
    c = client_or_404(client_id)
    data, errors = clean_client_form(request.form, existing=c)
    if errors:
        for e in errors:
            flash(e, "err")
    else:
        db.update_client(client_id, data)
        flash("Settings saved.", "ok")
    return redirect(url_for("client_detail", client_id=client_id))


@app.post("/clients/<int:client_id>/delete")
def client_delete(client_id):
    client_or_404(client_id)
    db.delete_client(client_id)
    flash("Client deleted.", "ok")
    return redirect(url_for("dashboard"))


@app.post("/clients/<int:client_id>/run")
def client_run(client_id):
    client_or_404(client_id)
    run_id = pipeline.submit_manual_run(client_id)
    if run_id is None:
        flash("This client already has a manual run in progress.", "err")
        return redirect(request.referrer or url_for("dashboard"))
    flash("Started an extra video outside today's plan.", "ok")
    return redirect(url_for("run_detail", run_id=run_id))


# ---------------------------------------------------------------- assets
@app.post("/assets/upload")
def asset_upload():
    client_id = request.form.get("client_id", type=int)   # empty = default for all clients
    kind = request.form.get("kind")
    if kind not in ("footer", "outro"):
        abort(400)
    if client_id:
        client_or_404(client_id)
    f = request.files.get("file")
    try:
        if not f or not f.filename:
            raise ValueError("Choose a file first.")
        save_asset(client_id, kind, f)
        flash(f"{kind.title()} uploaded.", "ok")
    except ValueError as exc:
        flash(str(exc), "err")
    return redirect(url_for("client_detail", client_id=client_id) if client_id else url_for("assets_page"))


@app.post("/assets/delete")
def asset_delete():
    client_id = request.form.get("client_id", type=int)
    kind = request.form.get("kind")
    if kind not in ("footer", "outro"):
        abort(400)
    old = db.delete_asset(client_id, kind)
    if old:
        Path(old).unlink(missing_ok=True)
    flash(f"{kind.title()} removed.", "ok")
    return redirect(url_for("client_detail", client_id=client_id) if client_id else url_for("assets_page"))


@app.get("/assets")
def assets_page():
    return render_template("assets.html", defaults={k: db.get_asset(None, k) for k in ("footer", "outro")})


@app.get("/assets/file/<int:asset_id>")
def asset_file(asset_id):
    with db.conn() as c:
        row = c.execute("SELECT path FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row or not Path(row["path"]).exists():
        abort(404)
    return send_file(row["path"], conditional=True)


# ---------------------------------------------------------------- YouTube
@app.get("/clients/<int:client_id>/youtube/connect")
def yt_connect(client_id):
    c = client_or_404(client_id)
    try:
        return redirect(youtube.authorization_url(c))
    except youtube.YouTubeError as exc:
        flash(str(exc), "err")
        return redirect(url_for("client_detail", client_id=client_id))


@app.get("/youtube/callback")
def yt_callback():
    state, code = request.args.get("state", ""), request.args.get("code", "")
    if request.args.get("error") or not code:
        flash(f"YouTube connection cancelled: {request.args.get('error', 'no code returned')}", "err")
        return redirect(url_for("dashboard"))
    try:
        res = youtube.complete_authorization(state, code)
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not connect YouTube: {exc}", "err")
        return redirect(url_for("dashboard"))
    db.update_client(res["client_id"], {"yt_refresh_enc": crypto.encrypt(res["refresh_token"]),
                                        "yt_channel_id": res["channel_id"],
                                        "yt_channel_title": res["channel_title"], "yt_status": ""})
    flash(f"Connected to YouTube channel \u201c{res['channel_title']}\u201d.", "ok")
    return redirect(url_for("client_detail", client_id=res["client_id"]))


@app.post("/clients/<int:client_id>/youtube/disconnect")
def yt_disconnect(client_id):
    client_or_404(client_id)
    db.update_client(client_id, {"yt_refresh_enc": "", "yt_channel_id": "", "yt_channel_title": "", "yt_status": ""})
    flash("YouTube disconnected.", "ok")
    return redirect(url_for("client_detail", client_id=client_id))


@app.post("/clients/<int:client_id>/youtube/secrets")
def yt_secrets(client_id):
    client_or_404(client_id)
    f = request.files.get("secrets_file")
    if not f or not f.filename:
        flash("Choose the client_secret.json file first.", "err")
        return redirect(url_for("client_detail", client_id=client_id))
    try:
        text = f.read().decode("utf-8")
        youtube.parse_secrets_json(text)   # validates shape
        db.update_client(client_id, {"google_secrets_enc": crypto.encrypt(text)})
        flash("This client now uses its own Google Cloud project. Reconnect YouTube to apply it.", "ok")
    except (youtube.YouTubeError, UnicodeDecodeError) as exc:
        flash(str(exc), "err")
    return redirect(url_for("client_detail", client_id=client_id))


@app.post("/clients/<int:client_id>/youtube/secrets/clear")
def yt_secrets_clear(client_id):
    client_or_404(client_id)
    db.update_client(client_id, {"google_secrets_enc": ""})
    flash("Reverted to the shared Google project. Reconnect YouTube to apply it.", "ok")
    return redirect(url_for("client_detail", client_id=client_id))


# ---------------------------------------------------------------- runs
@app.get("/runs/<int:run_id>")
def run_detail(run_id):
    run = db.get_run(run_id)
    if not run:
        abort(404)
    script = json.loads(run["script_json"]) if run["script_json"] else None
    has_video = bool(run["final_path"] and Path(run["final_path"]).exists())
    has_thumb = bool(run["thumb_path"] and Path(run["thumb_path"]).exists())
    return render_template("run.html", run=run, script=script, has_video=has_video, has_thumb=has_thumb,
                           active=run["status"] in ("queued", "running", "publishing"))


@app.get("/runs/<int:run_id>/video")
def run_video(run_id):
    run = db.get_run(run_id)
    if not run or not run["final_path"] or not Path(run["final_path"]).exists():
        abort(404)
    return send_file(run["final_path"], mimetype="video/mp4", conditional=True)


@app.get("/runs/<int:run_id>/thumbnail")
def run_thumbnail(run_id):
    run = db.get_run(run_id)
    if not run or not run["thumb_path"] or not Path(run["thumb_path"]).exists():
        abort(404)
    return send_file(run["thumb_path"], mimetype="image/jpeg", conditional=True)


@app.post("/runs/<int:run_id>/delete")
def run_delete(run_id):
    run = db.get_run(run_id)
    if not run:
        abort(404)
    if run["status"] in ("running", "publishing"):
        flash("Can't delete a run while it's in progress. Wait for it to finish or fail, then delete it.", "err")
        return redirect(request.referrer or url_for("dashboard"))
    client_id = run["client_id"]
    workdir = config.RUNS_DIR / str(run_id)
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    db.delete_run(run_id)
    flash(f"Run #{run_id} deleted.", "ok")
    dest = request.referrer or ""
    if f"/runs/{run_id}" in dest:
        return redirect(url_for("client_detail", client_id=client_id))
    return redirect(dest or url_for("dashboard"))


@app.post("/runs/<int:run_id>/publish")
def run_publish(run_id):
    if not pipeline.submit_manual_publish(run_id):
        flash("This video is not ready to publish (missing file or already in progress).", "err")
    return redirect(url_for("run_detail", run_id=run_id))


if __name__ == "__main__":
    db.init()
    ticker.start()
    if not config.ADMIN_PASSWORD:
        print("WARNING: ADMIN_PASSWORD is empty. Only run this on localhost.")
    app.run(host=config.HOST, port=config.PORT, debug=False, use_reloader=False, threaded=True)