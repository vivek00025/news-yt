"""Client for the Manus REST API v2 (https://open.manus.ai/docs/v2/introduction).

Flow: task.create -> poll task.listMessages -> when the agent stops, find the
MP4 (and thumbnail image) attachments on assistant messages -> download them.
"""
import time
from pathlib import Path

import requests

import config

VIDEO_EXT = (".mp4", ".mov", ".webm", ".m4v")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
MAX_NUDGES = 3


class ManusError(RuntimeError):
    pass


class ManusTaskFailed(ManusError):
    """The task itself failed or produced nothing: a retry needs a NEW task (not just more waiting)."""


class ManusClient:
    def __init__(self, api_key: str, base_url: str | None = None):
        if not api_key:
            raise ManusError("No Manus API key saved for this client.")
        self.key = api_key
        self.base = (base_url or config.MANUS_BASE_URL).rstrip("/")
        if not self.base.startswith(("http://", "https://")):
            raise ManusError(
                f"MANUS_BASE_URL in your .env is not a valid URL (currently '{self.base[:40]}...'). "
                "It should normally be left as https://api.manus.ai. The Manus API key itself does NOT "
                "go in .env - it is entered per client on that client's page in the app.")
        self.headers = {"x-manus-api-key": api_key, "Content-Type": "application/json"}

    # ---- low level ----
    def _request(self, method, path, **kw):
        try:
            r = requests.request(method, f"{self.base}{path}", headers=self.headers, timeout=45, **kw)
        except requests.RequestException as exc:
            raise ManusError(f"Network error talking to Manus: {exc}") from exc
        try:
            data = r.json()
        except ValueError:
            raise ManusError(f"Manus returned non-JSON (HTTP {r.status_code}): {r.text[:200]}")
        if not r.ok or data.get("ok") is False:
            err = data.get("error") or {}
            raise ManusError(f"Manus API error [{err.get('code', r.status_code)}]: "
                             f"{err.get('message') or r.text[:200]}")
        return data

    # ---- endpoints ----
    def create_task(self, prompt: str, title: str = "", profile: str = "standard") -> dict:
        body = {"message": {"content": prompt}, "agent_profile": profile or "standard",
                "interactive_mode": False, "hide_in_task_list": False}
        if title:
            body["title"] = title[:120]
        return self._request("POST", "/v2/task.create", json=body)

    def send_message(self, task_id: str, text: str):
        return self._request("POST", "/v2/task.sendMessage",
                             json={"task_id": task_id, "message": {"content": text}})

    def list_messages(self, task_id: str) -> list[dict]:
        events, cursor = [], None
        for _ in range(20):
            params = {"task_id": task_id, "order": "asc", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/v2/task.listMessages", params=params)
            events += data.get("messages", [])
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return events

    # ---- helpers ----
    @staticmethod
    def _ts(value) -> float:
        """Manus sometimes sends 'timestamp' as a number, sometimes as a numeric string."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _latest_status(cls, events, since_ms=0):
        found = None
        for ev in events:
            if ev.get("type") == "status_update" and cls._ts(ev.get("timestamp", 0)) >= since_ms:
                found = ev["status_update"]
        return found

    @staticmethod
    def _find_video(events):
        for ev in reversed(events):
            if ev.get("type") != "assistant_message":
                continue
            for att in (ev["assistant_message"].get("attachments") or []):
                name = (att.get("filename") or "").lower()
                ctype = (att.get("content_type") or "").lower()
                if att.get("url") and (ctype.startswith("video/") or name.endswith(VIDEO_EXT)):
                    return att
        return None

    @staticmethod
    def _find_image(events):
        """Newest image attachment, preferring a file whose name contains 'thumb'."""
        images = []
        for ev in events:
            if ev.get("type") != "assistant_message":
                continue
            for att in (ev["assistant_message"].get("attachments") or []):
                name = (att.get("filename") or "").lower()
                ctype = (att.get("content_type") or "").lower()
                if att.get("url") and (att.get("type") == "image" or ctype.startswith("image/")
                                       or name.endswith(IMAGE_EXT)):
                    images.append(att)
        named = [a for a in images if "thumb" in (a.get("filename") or "").lower()]
        pool = named or images
        return pool[-1] if pool else None

    @staticmethod
    def _last_text(events, kind="assistant_message"):
        for ev in reversed(events):
            if ev.get("type") == kind and ev.get(kind, {}).get("content"):
                return ev[kind]["content"][:400]
        return ""

    def wait_for_outputs(self, task_id: str, want_thumbnail=True, log=print, timeout_s=None, poll_s=None):
        """Poll until Manus stops, then return (video_attachment, thumbnail_attachment_or_None)."""
        timeout_s = timeout_s or config.MANUS_TIMEOUT_MINUTES * 60
        poll_s = poll_s or config.MANUS_POLL_SECONDS
        deadline = time.time() + timeout_s
        since_ms, nudges, net_errors, last_state = 0, 0, 0, None

        while time.time() < deadline:
            time.sleep(poll_s)
            try:
                events = self.list_messages(task_id)
                net_errors = 0
            except ManusError as exc:
                net_errors += 1
                log(f"Poll failed ({net_errors}/6): {exc}")
                if net_errors >= 6:
                    raise
                continue

            st = self._latest_status(events, since_ms)
            if not st:
                continue
            state = st.get("agent_status")
            if state != last_state:
                log(f"Manus status: {state}" + (f" - {st['brief']}" if st.get("brief") else ""))
                last_state = state

            if state == "running":
                continue
            if state == "error":
                raise ManusTaskFailed("Manus task failed: " +
                                      (self._last_text(events, "error_message") or "unknown error"))
            if state == "waiting":
                detail = st.get("status_detail") or {}
                wtype = detail.get("waiting_for_event_type", "")
                if wtype in ("messageAskUser", "cascadeAskUser") and nudges < MAX_NUDGES:
                    log(f"Manus asked a question; answering automatically: {self._last_text(events)[:150]}")
                    self.send_message(task_id, "Proceed with your best judgement. Do not ask further "
                                      "questions. Deliver the finished MP4 and the thumbnail image as attachments.")
                    nudges, since_ms, last_state = nudges + 1, int(time.time() * 1000), None
                    continue
                if wtype == "cascadeJobCall" and nudges < MAX_NUDGES:
                    log("Manus is waiting on a sub-task approval; approving automatically.")
                    self.send_message(task_id, "Approved - go ahead and continue with that sub-task. Do not "
                                      "ask for further approvals. Deliver the finished MP4 and the thumbnail "
                                      "image as attachments.")
                    nudges, since_ms, last_state = nudges + 1, int(time.time() * 1000), None
                    continue
                raise ManusTaskFailed(f"Manus is waiting on '{wtype or 'input'}', which this app will not "
                                      "auto-approve.")
            if state == "stopped":
                video = self._find_video(events)
                thumb = self._find_image(events) if want_thumbnail else None
                if video and (thumb or not want_thumbnail):
                    return video, thumb
                if nudges < MAX_NUDGES:
                    if not video:
                        log("No MP4 attached yet; asking Manus to attach it.")
                        msg = ("Your last message did not include the MP4 as a file attachment. Please attach "
                               "the finished video (video.mp4)" + (" and the thumbnail image (thumbnail.png)."
                               if want_thumbnail else "."))
                    else:
                        log("Video received but no thumbnail; asking Manus for it.")
                        msg = ("The video is fine. Please also attach the thumbnail image "
                               "(1280x720 PNG or JPG) as a file attachment.")
                    self.send_message(task_id, msg)
                    nudges, since_ms, last_state = nudges + 1, int(time.time() * 1000), None
                    continue
                if video:
                    log("Manus did not return a thumbnail; a frame from the video will be used instead.")
                    return video, None
                raise ManusTaskFailed("Manus finished but returned no video file. Last message: "
                                      + (self._last_text(events) or "(none)"))
        raise ManusError(f"Timed out after {timeout_s // 60} minutes waiting for Manus.")

    def download(self, url: str, dest: Path) -> Path:
        for headers in ({}, {"x-manus-api-key": self.key}):  # presigned URLs first, then authenticated
            try:
                with requests.get(url, headers=headers, stream=True, timeout=(15, 180)) as r:
                    if r.status_code in (401, 403) and not headers:
                        continue
                    r.raise_for_status()
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
                if dest.stat().st_size < 10_000:
                    raise ManusError("Downloaded video is suspiciously small.")
                return dest
            except requests.RequestException as exc:
                raise ManusError(f"Could not download the video: {exc}") from exc
        raise ManusError("Video URL rejected with 401/403.")