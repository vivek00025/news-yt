"""Per-client YouTube connection (OAuth 2.0 web flow) and video + thumbnail upload.

Each client can optionally use its own Google Cloud project (own OAuth client JSON, own API quota).
Otherwise the shared client_secret.json from .env is used."""
import json
import os
import secrets
import threading
import time
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

import config
import crypto

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube",   # needed for thumbnails.set
]
TOKEN_URI = "https://oauth2.googleapis.com/token"

os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
if config.BASE_URL.startswith("http://"):  # local development only
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

_pending: dict[str, dict] = {}   # oauth state -> {client_id, cfg, verifier, ts}
_lock = threading.Lock()


class YouTubeError(RuntimeError):
    def __init__(self, msg, permanent=False, reauth=False):
        super().__init__(msg)
        self.permanent = permanent   # retrying will not help
        self.reauth = reauth         # the channel must be reconnected


# ---------- credentials source ----------
def parse_secrets_json(text: str) -> dict:
    try:
        cfg = json.loads(text).get("web")
    except (ValueError, AttributeError):
        cfg = None
    if not cfg or not cfg.get("client_id") or not cfg.get("client_secret"):
        raise YouTubeError('That file is not a Google OAuth client of type "Web application".')
    return cfg


def _cfg(client=None) -> dict:
    if client is not None and client["google_secrets_enc"]:
        return parse_secrets_json(crypto.decrypt(client["google_secrets_enc"]))
    if not config.GOOGLE_CLIENT_SECRETS_FILE.exists():
        raise YouTubeError(f"Google OAuth file not found: {config.GOOGLE_CLIENT_SECRETS_FILE}")
    return parse_secrets_json(config.GOOGLE_CLIENT_SECRETS_FILE.read_text())


def is_configured(client=None) -> bool:
    if client is not None and client["google_secrets_enc"]:
        return True
    return config.GOOGLE_CLIENT_SECRETS_FILE.exists()


def _flow(cfg: dict, state=None) -> Flow:
    return Flow.from_client_config({"web": cfg}, scopes=SCOPES,
                                   redirect_uri=f"{config.BASE_URL}/youtube/callback", state=state)


# ---------- connect ----------
def authorization_url(client) -> str:
    cfg = _cfg(client)
    flow = _flow(cfg)
    state = secrets.token_urlsafe(24)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent",
                                    include_granted_scopes="true", state=state)
    with _lock:
        for k in [k for k, v in _pending.items() if time.time() - v["ts"] > 900]:
            _pending.pop(k, None)
        _pending[state] = {"client_id": client["id"], "cfg": cfg,
                           "verifier": getattr(flow, "code_verifier", None), "ts": time.time()}
    return url


def complete_authorization(state: str, code: str) -> dict:
    with _lock:
        pending = _pending.pop(state, None)
    if not pending:
        raise YouTubeError("This sign-in link expired or was already used. Start again.")
    flow = _flow(pending["cfg"], state=state)
    if pending["verifier"]:
        flow.code_verifier = pending["verifier"]
    flow.fetch_token(code=code)
    creds = flow.credentials
    if not creds.refresh_token:
        raise YouTubeError("Google did not return a refresh token. Remove this app at "
                           "myaccount.google.com/permissions and connect again.")
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        raise YouTubeError("That Google account has no YouTube channel.")
    return {"client_id": pending["client_id"], "refresh_token": creds.refresh_token,
            "channel_id": items[0]["id"], "channel_title": items[0]["snippet"]["title"]}


# ---------- upload ----------
def _credentials(client, refresh_token: str) -> Credentials:
    cfg = _cfg(client)
    return Credentials(token=None, refresh_token=refresh_token, token_uri=TOKEN_URI,
                       client_id=cfg["client_id"], client_secret=cfg["client_secret"], scopes=SCOPES)


def _sanitize(text: str, limit: int) -> str:
    return text.replace("<", "").replace(">", "").strip()[:limit]


def _tags(tags: list[str]) -> list[str]:
    out, total = [], 0
    for t in tags:
        t = _sanitize(t, 40)
        if t and total + len(t) + 1 <= 450:
            out.append(t)
            total += len(t) + 1
    return out


def upload(client, path: Path, title: str, description: str, tags: list[str],
           thumbnail: Path | None = None, log=print) -> tuple[str, str]:
    """Uploads the video, then sets the thumbnail. Returns (video_id, thumbnail_note)."""
    refresh = crypto.decrypt(client["yt_refresh_enc"])
    if not refresh:
        raise YouTubeError("YouTube is not connected for this client.", permanent=True, reauth=True)
    try:
        yt = build("youtube", "v3", credentials=_credentials(client, refresh), cache_discovery=False)
        status = {"privacyStatus": client["privacy"], "selfDeclaredMadeForKids": False}
        if client["ai_disclosure"]:
            status["containsSyntheticMedia"] = True
        body = {
            "snippet": {"title": _sanitize(title, 100), "description": _sanitize(description, 4900),
                        "tags": _tags(tags), "categoryId": str(client["category_id"] or "25")},
            "status": status,
        }
        video_id = _insert(yt, body, path, log)
    except RefreshError as exc:
        raise YouTubeError("YouTube access was revoked or expired. Reconnect the channel on the client "
                           f"page. ({exc})", permanent=True, reauth=True) from exc
    except HttpError as exc:
        raise _http_error(exc) from exc

    note = "no thumbnail"
    if thumbnail and Path(thumbnail).exists():
        try:
            yt.thumbnails().set(videoId=video_id,
                                media_body=MediaFileUpload(str(thumbnail), mimetype="image/jpeg")).execute()
            note = "thumbnail set"
        except HttpError as exc:
            text = str(exc.content)
            if exc.resp.status == 403:
                note = ("thumbnail rejected: the channel must be verified (youtube.com/verify) "
                        "to use custom thumbnails")
            else:
                note = f"thumbnail failed: HTTP {exc.resp.status} {text[:120]}"
            log("Warning: " + note)
        except Exception as exc:  # noqa: BLE001 - the video is already up; never fail the run for this
            note = f"thumbnail failed: {exc}"
            log("Warning: " + note)
    return video_id, note


def _insert(yt, body, path, log, allow_fallback=True):
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp, retries = None, 0
    while resp is None:
        try:
            progress, resp = req.next_chunk()
            if progress:
                log(f"Upload {int(progress.progress() * 100)}%")
        except HttpError as exc:
            if exc.resp.status in (500, 502, 503, 504) and retries < 5:
                retries += 1
                time.sleep(2 ** retries)
                continue
            if (exc.resp.status == 400 and allow_fallback and "containsSyntheticMedia" in str(exc.content)):
                log("YouTube rejected the AI-disclosure flag; retrying without it.")
                body["status"].pop("containsSyntheticMedia", None)
                return _insert(yt, body, path, log, allow_fallback=False)
            raise
        except (ConnectionError, TimeoutError) as exc:
            if retries >= 5:
                raise YouTubeError(f"Network error during upload: {exc}") from exc
            retries += 1
            time.sleep(2 ** retries)
    return resp["id"]


def _http_error(exc: HttpError) -> YouTubeError:
    text = str(exc.content)
    if "quotaExceeded" in text:
        return YouTubeError("YouTube API daily quota exceeded (an upload costs about 1,600 units of the default "
                            "10,000 per Google Cloud project per day). Use a separate Google project for this "
                            "client or request a quota increase.", permanent=True)
    if "uploadLimitExceeded" in text:
        return YouTubeError("This YouTube channel hit its daily upload limit.", permanent=True)
    if exc.resp.status in (401, 403) and ("invalid_grant" in text or "authError" in text):
        return YouTubeError("YouTube rejected the saved sign-in. Reconnect the channel.", permanent=True, reauth=True)
    return YouTubeError(f"YouTube API error {exc.resp.status}: {text[:300]}", permanent=exc.resp.status in (400, 403))
