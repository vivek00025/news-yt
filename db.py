import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    -- news
    country TEXT NOT NULL DEFAULT 'US',
    language TEXT NOT NULL DEFAULT 'en',
    news_query TEXT NOT NULL DEFAULT 'politics',
    extra_feeds TEXT NOT NULL DEFAULT '',
    headlines_count INTEGER NOT NULL DEFAULT 8,
    -- script / video
    script_language TEXT NOT NULL DEFAULT 'English',
    style_notes TEXT NOT NULL DEFAULT '',
    duration INTEGER NOT NULL DEFAULT 20,
    aspect TEXT NOT NULL DEFAULT '9:16',
    -- manus
    manus_api_key_enc TEXT NOT NULL DEFAULT '',
    manus_profile TEXT NOT NULL DEFAULT 'standard',
    -- daily plan
    videos_per_day INTEGER NOT NULL DEFAULT 1,
    slot_times TEXT NOT NULL DEFAULT '09:00,13:00,18:00,21:00',
    generate_time TEXT NOT NULL DEFAULT '05:00',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    -- youtube
    privacy TEXT NOT NULL DEFAULT 'private',
    auto_publish INTEGER NOT NULL DEFAULT 1,
    category_id TEXT NOT NULL DEFAULT '25',
    default_tags TEXT NOT NULL DEFAULT '',
    description_footer TEXT NOT NULL DEFAULT '',
    ai_disclosure INTEGER NOT NULL DEFAULT 1,
    yt_refresh_enc TEXT NOT NULL DEFAULT '',
    yt_channel_id TEXT NOT NULL DEFAULT '',
    yt_channel_title TEXT NOT NULL DEFAULT '',
    yt_status TEXT NOT NULL DEFAULT '',            -- '' ok, 'reauth' = must reconnect
    google_secrets_enc TEXT NOT NULL DEFAULT '',   -- optional per-client Google Cloud project
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,  -- NULL = default for all clients
    kind TEXT NOT NULL CHECK (kind IN ('footer','outro')),
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    trigger TEXT NOT NULL DEFAULT 'manual',        -- manual | plan
    status TEXT NOT NULL DEFAULT 'queued',         -- queued|running|ready|publishing|done|failed|skipped
    step TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    plan_date TEXT,                                -- client-local date this video belongs to
    slot INTEGER,                                  -- 1..4 (video number of the day)
    publish_at TEXT,                               -- UTC time the upload should happen
    attempts INTEGER NOT NULL DEFAULT 0,
    pub_attempts INTEGER NOT NULL DEFAULT 0,
    retry_after TEXT,
    script_json TEXT,
    manus_task_id TEXT,
    manus_task_url TEXT,
    final_path TEXT,
    thumb_path TEXT,
    thumb_status TEXT,
    youtube_id TEXT,
    error TEXT,
    log TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS used_articles (
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    hash TEXT NOT NULL,
    title TEXT NOT NULL,
    used_at TEXT NOT NULL,
    PRIMARY KEY (client_id, hash)
);
"""

# Columns added after the first release: applied to older databases on start.
NEW_COLUMNS = {
    "clients": {
        "videos_per_day": "INTEGER NOT NULL DEFAULT 1",
        "slot_times": "TEXT NOT NULL DEFAULT '09:00,13:00,18:00,21:00'",
        "generate_time": "TEXT NOT NULL DEFAULT '05:00'",
        "description_footer": "TEXT NOT NULL DEFAULT ''",
        "yt_status": "TEXT NOT NULL DEFAULT ''",
        "google_secrets_enc": "TEXT NOT NULL DEFAULT ''",
    },
    "runs": {
        "plan_date": "TEXT", "slot": "INTEGER", "publish_at": "TEXT",
        "attempts": "INTEGER NOT NULL DEFAULT 0", "pub_attempts": "INTEGER NOT NULL DEFAULT 0",
        "retry_after": "TEXT", "thumb_path": "TEXT", "thumb_status": "TEXT",
    },
}

CLIENT_FIELDS = (
    "name", "enabled", "country", "language", "news_query", "extra_feeds",
    "headlines_count", "script_language", "style_notes", "duration", "aspect",
    "manus_api_key_enc", "manus_profile", "videos_per_day", "slot_times", "generate_time",
    "timezone", "privacy", "auto_publish", "category_id", "default_tags", "description_footer",
    "ai_disclosure", "yt_refresh_enc", "yt_channel_id", "yt_channel_title", "yt_status",
    "google_secrets_enc",
)
RUN_FIELDS = (
    "status", "step", "finished_at", "publish_at", "attempts", "pub_attempts", "retry_after",
    "script_json", "manus_task_id", "manus_task_url", "final_path", "thumb_path", "thumb_status",
    "youtube_id", "error",
)
RUN_SELECT = ("SELECT r.*, c.name AS client_name, c.timezone AS client_tz, c.auto_publish AS client_auto "
              "FROM runs r JOIN clients c ON c.id=r.client_id")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn():
    c = sqlite3.connect(config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        for table, cols in NEW_COLUMNS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols.items():
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_plan ON runs(client_id, plan_date, slot) "
                  "WHERE plan_date IS NOT NULL")
        # Recover from a crash or restart: unfinished work goes back in the queue.
        c.execute("UPDATE runs SET status='queued', step='Resuming after restart', retry_after=NULL "
                  "WHERE status='running'")
        c.execute("UPDATE runs SET status='ready', step='' WHERE status='publishing'")


# ---------- clients ----------
def list_clients():
    with conn() as c:
        return c.execute("SELECT * FROM clients ORDER BY name COLLATE NOCASE").fetchall()


def get_client(client_id):
    with conn() as c:
        return c.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()


def create_client(data: dict) -> int:
    cols = [k for k in data if k in CLIENT_FIELDS]
    with conn() as c:
        cur = c.execute(
            f"INSERT INTO clients ({','.join(cols)}, created_at) VALUES ({','.join('?' * len(cols))}, ?)",
            [data[k] for k in cols] + [now()])
        return cur.lastrowid


def update_client(client_id, data: dict):
    cols = [k for k in data if k in CLIENT_FIELDS]
    if not cols:
        return
    with conn() as c:
        c.execute(f"UPDATE clients SET {','.join(f'{k}=?' for k in cols)} WHERE id=?",
                  [data[k] for k in cols] + [client_id])


def delete_client(client_id):
    with conn() as c:
        c.execute("DELETE FROM clients WHERE id=?", (client_id,))


# ---------- assets ----------
def get_asset(client_id, kind):
    with conn() as c:
        return c.execute("SELECT * FROM assets WHERE client_id IS ? AND kind=?", (client_id, kind)).fetchone()


def resolve_asset(client_id, kind):
    """Client's own asset, else the global default, else None."""
    return get_asset(client_id, kind) or get_asset(None, kind)


def set_asset(client_id, kind, filename, path):
    with conn() as c:
        c.execute("DELETE FROM assets WHERE client_id IS ? AND kind=?", (client_id, kind))
        c.execute("INSERT INTO assets (client_id, kind, filename, path, created_at) VALUES (?,?,?,?,?)",
                  (client_id, kind, filename, str(path), now()))


def delete_asset(client_id, kind):
    with conn() as c:
        row = c.execute("SELECT path FROM assets WHERE client_id IS ? AND kind=?", (client_id, kind)).fetchone()
        c.execute("DELETE FROM assets WHERE client_id IS ? AND kind=?", (client_id, kind))
        return row["path"] if row else None


# ---------- runs ----------
def create_run(client_id, trigger="manual", plan_date=None, slot=None, publish_at=None,
               status="queued", error=None):
    try:
        with conn() as c:
            cur = c.execute(
                "INSERT INTO runs (client_id, trigger, status, started_at, plan_date, slot, publish_at, error, finished_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (client_id, trigger, status, now(), plan_date, slot, publish_at, error,
                 now() if status in ("skipped", "failed") else None))
            return cur.lastrowid
    except sqlite3.IntegrityError:      # this slot was already planned (race between two ticks)
        return None


def update_run(run_id, **kw):
    cols = [k for k in kw if k in RUN_FIELDS]
    if not cols:
        return
    with conn() as c:
        c.execute(f"UPDATE runs SET {','.join(f'{k}=?' for k in cols)} WHERE id=?",
                  [kw[k] for k in cols] + [run_id])


def delete_run(run_id):
    with conn() as c:
        c.execute("DELETE FROM runs WHERE id=?", (run_id,))


def append_log(run_id, message):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n"
    with conn() as c:
        c.execute("UPDATE runs SET log = log || ? WHERE id=?", (line, run_id))


def get_run(run_id):
    with conn() as c:
        return c.execute(RUN_SELECT + " WHERE r.id=?", (run_id,)).fetchone()


def list_runs(client_id=None, limit=15):
    q, args = RUN_SELECT, []
    if client_id:
        q += " WHERE r.client_id=?"
        args.append(client_id)
    q += " ORDER BY r.id DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return c.execute(q, args).fetchall()


def runs_for_plan(client_id, plan_date):
    with conn() as c:
        return c.execute(RUN_SELECT + " WHERE r.client_id=? AND r.plan_date=? ORDER BY r.slot",
                         (client_id, plan_date)).fetchall()


def queued_runs(now_iso):
    with conn() as c:
        return c.execute(
            RUN_SELECT + " WHERE r.status='queued' AND c.enabled=1 AND (r.retry_after IS NULL OR r.retry_after<=?) "
            "ORDER BY COALESCE(r.publish_at, r.started_at), r.id", (now_iso,)).fetchall()


def ready_runs(now_iso):
    """Finished videos of auto-publish clients whose upload time has arrived."""
    with conn() as c:
        return c.execute(
            RUN_SELECT + " WHERE r.status='ready' AND c.enabled=1 AND c.auto_publish=1 "
            "AND (r.publish_at IS NULL OR r.publish_at<=?) ORDER BY r.publish_at", (now_iso,)).fetchall()


def waiting_runs():
    """Everything that could still be expired for being too late."""
    with conn() as c:
        return c.execute(RUN_SELECT + " WHERE r.status IN ('queued','ready') AND r.publish_at IS NOT NULL").fetchall()


def has_open_manual(client_id) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM runs WHERE client_id=? AND trigger='manual' "
                         "AND status IN ('queued','running')", (client_id,)).fetchone() is not None


def recent_titles(client_id, hours=36):
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    out = []
    with conn() as c:
        for r in c.execute("SELECT script_json FROM runs WHERE client_id=? AND script_json IS NOT NULL "
                           "AND started_at>=?", (client_id, since)):
            try:
                s = json.loads(r["script_json"])
                out.append(s.get("topic") or s.get("title") or "")
            except ValueError:
                pass
    return [t for t in out if t]


def old_finished_runs(before_iso):
    with conn() as c:
        return c.execute("SELECT id, final_path, thumb_path FROM runs WHERE status IN ('done','failed','skipped') "
                         "AND finished_at IS NOT NULL AND finished_at<? "
                         "AND (final_path IS NOT NULL OR thumb_path IS NOT NULL)", (before_iso,)).fetchall()


# ---------- used articles ----------
def used_hashes(client_id) -> set:
    with conn() as c:
        return {r["hash"] for r in c.execute("SELECT hash FROM used_articles WHERE client_id=?", (client_id,))}


def mark_used(client_id, articles):
    with conn() as c:
        for a in articles:
            c.execute("INSERT OR IGNORE INTO used_articles (client_id, hash, title, used_at) VALUES (?,?,?,?)",
                      (client_id, a["hash"], a["title"], now()))


def unmark_used(client_id, hashes):
    with conn() as c:
        for h in hashes:
            c.execute("DELETE FROM used_articles WHERE client_id=? AND hash=?", (client_id, h))