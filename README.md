# Newsreel: fully automatic daily political news videos for many clients

Runs forever in the background. For every enabled client, every day, it:

1. Fetches the latest political headlines (Google News RSS, plus optional custom feeds), skipping stories already used.
2. Groq writes the script, an SEO title/description/tags, and a thumbnail brief.
3. Manus generates the video **and** the thumbnail from one prompt that states the length (10/20/30s or any custom length) and the 9:16 or 16:9 ratio. Each client uses its own Manus API key.
4. ffmpeg fits the video to 1080x1920 or 1920x1080, draws your **footer** over the bottom, and appends your **outro**. The thumbnail is cropped to YouTube's 1280x720 and compressed under 2 MB.
5. The finished video and thumbnail are uploaded to the client's own YouTube channel, at that video's scheduled time.

Each client picks **1, 2, 3 or 4 videos a day**, with an exact upload time for each one (e.g. video 1 at 09:00, video 2 at 18:30). The app checks every 30 seconds for work to do, so as long as `python app.py` keeps running, new videos appear and go out on schedule every day with no manual step and no cron job.

## Setup

```bash
# 1. Install ffmpeg (https://ffmpeg.org) so `ffmpeg` and `ffprobe` are on PATH
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # then edit it
python app.py                                         # open http://localhost:5000
```

In `.env` set at least `GROQ_API_KEY`, `ADMIN_USER` and `ADMIN_PASSWORD`. Keep it running (systemd, Docker, pm2, a screen/tmux session) on a machine that stays on, since the daily schedule only advances while the process is alive.

### YouTube connection (one-time, in Google Cloud)

1. Create a project at https://console.cloud.google.com and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen. Add every Google account that owns a client channel as a **test user** while the app is in Testing (or publish the screen so connections don't expire after 7 days).
3. Create credentials: **OAuth client ID**, type **Web application**. Add the redirect URI `http://localhost:5000/youtube/callback` (use your real `BASE_URL` in production).
4. Download the JSON, save it as `client_secret.json` next to `app.py`. This is the default project shared by all clients.
5. On a client's page press **Connect YouTube** and sign in with that client's Google account. Each client stores its own encrypted refresh token.

Optionally, a client can use its **own** Google Cloud project (its own quota) instead of the shared one: upload that project's OAuth client JSON under "Use a separate Google Cloud project" on the client's page.

### Manus key (per client)

In Manus: Settings, API Integration, Create API Key. Paste it on the client's page. Videos and thumbnails for that client are generated only with that key and use that account's credits.

## Using it

* **Add client**: topic, country, language, video length (10/20/30s presets or a custom 5-180s length), aspect ratio, Manus key, videos per day (1-4) with an upload time for each, timezone, YouTube visibility, auto-publish on/off.
* **Today's plan**: each client's page shows today's videos, their scheduled times and their live status.
* **Footer and outro**: upload per client on the client page, or once under **Default branding** for everyone. A client's own file wins over the default. The thumbnail needs no upload: Manus generates it for every video automatically.
* **Upload automatically** off means every finished video waits on its run page until you press **Approve and upload**.
* **Run an extra video now**: generates one video immediately, outside the daily plan.
* Each run page shows a step-by-step progress bar, the script, SEO metadata, the thumbnail, the final video and a full log.
* Failed steps are retried automatically (network hiccups, temporary Manus/YouTube errors) with a backoff, up to `MAX_ATTEMPTS` (default 3). Failures that clearly can't succeed on retry (no API key, no news, YouTube not connected) fail immediately instead of wasting time. A video that still hasn't been generated `MAX_LATE_HOURS` (default 6) after its scheduled time is skipped rather than posted very late with stale news.

## Things to know before going live

* **YouTube API limits.** New API projects that have not passed Google's API compliance audit have uploaded videos locked to private. The default quota (10,000 units/day) allows about 6 uploads per day per Google Cloud project; running many clients at 3-4 videos/day may need a quota increase or separate projects per client (see above).
* **Manus is a general agent, not a dedicated video API.** The app asks it for one MP4 and one thumbnail image per task and downloads whatever it attaches. Results and cost depend on your Manus plan. Check the first videos of a new client before turning on auto-publish.
* **Political content.** Scripts are restricted to facts in the fetched headlines, attributed to their sources, and told not to fabricate footage or invent details. That lowers risk but does not remove it. The AI-disclosure box (on by default) sets YouTube's synthetic-content flag. Consider starting new clients on manual approval.
* **Custom thumbnails on YouTube require a verified channel** (phone verification at youtube.com/verify). If a channel isn't verified, thumbnail uploads are skipped with a note in the run log; the video itself still publishes.
* Secrets (Manus keys, YouTube tokens, optional per-client Google credentials) are encrypted in `data/newsreel.db` with a key in `data/.enc_key`. Back both up together. Use HTTPS if you host this on a server.
* Finished video/thumbnail files are deleted automatically after `RETENTION_DAYS` (default 10) to save disk space; the run history and YouTube links stay.

## Files

```
app.py               web dashboard and routes
pipeline.py          news -> script -> video+thumbnail -> edit -> upload, with retries
daily.py             computes each client's daily video slots and publish times
ticker.py            background loop: plans, dispatches, retries, expires, cleans up - forever
services/news.py     headline fetching
services/groq_script.py   Groq script + SEO metadata + Manus prompt
services/manus_client.py  Manus API v2 (video + thumbnail)
services/video_edit.py    ffmpeg footer + outro + thumbnail processing
services/youtube.py       per-client OAuth, video + thumbnail upload
data/                database, assets, per-run videos (created on first start)
```
