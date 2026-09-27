"""Turns headlines into a short-video script + YouTube SEO metadata + thumbnail brief with Groq,
then builds the exact prompt sent to Manus (video and thumbnail in one task)."""
import json
import re
import time

import requests

import config

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqError(RuntimeError):
    pass


def words_for(duration: int) -> int:
    """About 2.5 spoken words per second."""
    return max(10, round(int(duration) * 2.5))


def _max_tokens(duration: int) -> int:
    """The JSON reply grows with the video length (voiceover + a video_prompt covering the whole
    duration), so a fixed token budget truncates long videos. Scale it, with headroom."""
    words = words_for(duration)
    return max(2200, min(8000, 1200 + words * 4 + int(duration) * 12))


SYSTEM = """You are a scriptwriter and YouTube SEO editor for a short-form political news channel.

Content rules (hard):
- Use ONLY facts stated in the headlines provided. Never invent names, numbers, quotes, dates or causes.
- Stay neutral and factual. Attribute claims ("according to <source>"). No endorsements, insults, speculation
  about motives, or calls to vote or act.
- For an ongoing, unresolved event, say what is reported so far and that details may change.
- Never present a visual as real footage of a real event or a real person. Use graphics, maps, text cards,
  symbols or abstract/stock-style imagery.

SEO rules:
- title: aim for 55-70 characters (hard maximum 100). Put the main subject first (person, party, bill, place,
  event). Specific and accurate. No clickbait, no ALL CAPS words, no emoji, no misleading promises.
- description: the first two lines (about 150 characters) must state the story using its key search terms,
  because YouTube shows only that in search. Then 3-4 sentences of context from the headlines. End with one
  short line inviting viewers to subscribe for daily political news, in the script language.
  No URLs and no hashtags (they are added later).
- tags: 10-15 tags mixing exact search phrases ("<topic> news"), broader terms (politics, <country> politics)
  and names of people, parties and places mentioned. Each under 30 characters, without the # symbol.
- hashtags: 3-5 relevant hashtags, no spaces.
- thumbnail_text: at most 4 words in the script language. Punchy but accurate.
- thumbnail_prompt: one sentence describing a bold, high-contrast 16:9 thumbnail built from graphics, symbols,
  maps or illustration. Never a real person's likeness.

Keep video_prompt efficient regardless of length: describe it in beats of about 10-15 seconds each
(not second-by-second), so a longer video gets more beats, not a wall of detail per beat. This keeps the
whole JSON reply compact enough to generate in full.

Return ONLY one JSON object with exactly these keys:
{"topic": "...", "title": "...", "description": "...", "tags": ["..."], "hashtags": ["#..."],
 "voiceover": "narration text only, no stage directions",
 "video_prompt": "scene-by-scene visual direction with timestamps covering the whole duration",
 "thumbnail_text": "...", "thumbnail_prompt": "...", "used_headline_indexes": [0]}"""


def _user_prompt(client, headlines, duration, aspect, avoid, retry_note=""):
    lines = []
    for i, h in enumerate(headlines):
        when = h["published"].strftime("%Y-%m-%d %H:%M UTC") if h.get("published") else "unknown time"
        extra = f" | {h['summary']}" if h.get("summary") else ""
        lines.append(f"[{i}] {h['title']} (source: {h['source'] or 'unknown'}, {when}){extra}")
    avoid_txt = ""
    if avoid:
        avoid_txt = ("\nThe channel already covered these topics recently. Pick a DIFFERENT story:\n- "
                     + "\n- ".join(avoid[:8]) + "\n")
    return (
        f"Channel: {client['name']}\n"
        f"Script language: {client['script_language']}\n"
        f"Target country: {client['country']}\n"
        f"Video length: {duration} seconds, aspect ratio {aspect}.\n"
        f"Voiceover length: about {words_for(duration)} words (must fit {duration} seconds when read aloud).\n"
        f"Channel style notes: {client['style_notes'].strip() or 'None.'}\n{avoid_txt}\n"
        "Pick the single most newsworthy political story (or one tight group of related headlines) "
        "from this list and write everything for it.\n\nHEADLINES:\n" + "\n".join(lines) + retry_note)


class GroqTruncated(GroqError):
    """The reply was cut off by max_tokens before valid JSON was finished. Retrying with the same
    budget will fail the same way; the caller should raise the budget instead."""


class GroqInvalidJSON(GroqError):
    """The model's reply failed Groq's own JSON validation (not a truncation). A corrective retry
    with the same token budget can still succeed."""


def _call_groq(messages, max_tokens):
    if not config.GROQ_API_KEY:
        raise GroqError("GROQ_API_KEY is not set.")
    payload = {"model": config.GROQ_MODEL, "messages": messages, "temperature": 0.5,
               "response_format": {"type": "json_object"}, "max_tokens": max_tokens}
    headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}
    last = None
    for attempt in range(4):
        try:
            r = requests.post(GROQ_URL, json=payload, headers=headers, timeout=60)
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                time.sleep(2 * (attempt + 1) ** 2)
                continue
            if r.status_code == 400 and "json_validate_failed" in r.text:
                if "max completion tokens" in r.text:
                    raise GroqTruncated(f"Reply was cut off at {max_tokens} tokens before finishing.")
                raise GroqInvalidJSON(f"Groq could not produce valid JSON: {r.text[:200]}")
            if not r.ok:
                raise GroqError(f"Groq HTTP {r.status_code}: {r.text[:300]}")
            return r.json()["choices"][0]["message"]["content"]
        except requests.RequestException as exc:
            last = str(exc)
            time.sleep(2 * (attempt + 1))
    raise GroqError(f"Groq request failed after retries: {last}")


def _clean_seo(data: dict, headlines: list) -> dict:
    tags, seen, total = [], set(), 0
    for t in data.get("tags", []):
        t = re.sub(r"\s+", " ", str(t)).strip().lstrip("#")[:30]
        if t and t.lower() not in seen and total + len(t) + 1 <= 400:
            seen.add(t.lower())
            tags.append(t)
            total += len(t) + 1
    tags_out = tags[:15]
    hashtags = []
    for h in data.get("hashtags", []):
        h = "#" + re.sub(r"[^\w]", "", str(h))
        if len(h) > 1 and h not in hashtags:
            hashtags.append(h)
    data["tags"], data["hashtags"] = tags_out, hashtags[:5]
    data["title"] = re.sub(r"\s+", " ", data["title"]).strip()[:100]
    data["thumbnail_text"] = " ".join(str(data.get("thumbnail_text", "")).split()[:4])
    data["thumbnail_prompt"] = str(data.get("thumbnail_prompt", "")).strip() or \
        "Bold high-contrast news graphic with a simple symbol for the story"
    idx = [i for i in data.get("used_headline_indexes", []) if isinstance(i, int) and 0 <= i < len(headlines)]
    data["used_headline_indexes"] = idx or [0]
    return data


def generate_script(client, headlines, avoid_topics=(), log=print) -> dict:
    duration, aspect = int(client["duration"]), client["aspect"]
    target = words_for(duration)
    max_tokens = _max_tokens(duration)
    note, length_retry_used, max_attempts = "", False, 4
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        try:
            raw = _call_groq([{"role": "system", "content": SYSTEM},
                              {"role": "user", "content": _user_prompt(client, headlines, duration, aspect,
                                                                       list(avoid_topics), note)}], max_tokens)
        except GroqTruncated as exc:
            max_tokens = min(max_tokens * 2, 16000)
            log(f"Script attempt {attempt}: {exc} Retrying with a {max_tokens}-token budget.")
            max_attempts += 1   # this retry is a token-budget fix, not a content quality retry
            continue
        except GroqInvalidJSON as exc:
            note = "\n\nYour previous reply was not valid JSON. Return ONLY one valid JSON object, no other text."
            log(f"Script attempt {attempt}: {exc} Retrying.")
            continue
        try:
            data = json.loads(raw)
            for key in ("title", "description", "voiceover", "video_prompt"):
                if not str(data.get(key, "")).strip():
                    raise ValueError(f"missing '{key}'")
        except (ValueError, json.JSONDecodeError) as exc:
            note = f"\n\nYour previous reply was invalid ({exc}). Return valid JSON with every key."
            log(f"Script attempt {attempt} invalid: {exc}")
            continue
        n = len(re.findall(r"\S+", data["voiceover"]))
        if not (target * 0.75 <= n <= target * 1.25) and not length_retry_used:
            length_retry_used = True
            note = (f"\n\nYour voiceover had {n} words. Rewrite it to about {target} words "
                    f"so it fits {duration} seconds.")
            log(f"Voiceover was {n} words (target {target}); asking Groq to redo it.")
            continue
        data = _clean_seo(data, headlines)
        data["voiceover_words"] = n
        return data
    raise GroqError(f"Groq did not return a usable script after {attempt} attempts.")


def build_manus_prompt(client, script: dict) -> str:
    duration, aspect = int(client["duration"]), client["aspect"]
    w, h = config.FRAME_SIZES[aspect]
    orientation = "vertical" if aspect == "9:16" else "horizontal"
    text = script.get("thumbnail_text") or ""
    return f"""Create a {duration}-second {orientation} news video AND a YouTube thumbnail. Deliver BOTH as separate file attachments on your final message: (1) the MP4 video named video.mp4, (2) the thumbnail image named thumbnail.png.

VIDEO requirements (follow exactly):
- Aspect ratio {aspect} ({w}x{h}), MP4 (H.264 video, AAC audio), 30 fps.
- Total length {duration} seconds (plus or minus 1 second).
- Natural-sounding voiceover reading the script below, in sync with the visuals, with burned-in captions.
- Keep the bottom 15% of the frame free of captions and important visuals: a footer banner is added there later.
- Do NOT add an outro, end card, logo or watermark: those are added later.

THUMBNAIL requirements:
- 1280x720 (16:9) PNG or JPG under 2 MB, regardless of the video's aspect ratio.
- Bold, high-contrast, readable at small size. Large headline text: "{text}".
- Concept: {script.get('thumbnail_prompt', '')}
- Graphics, symbols, maps or illustration only. No real person's likeness, no logos, nothing misleading.

General:
- Do not ask me questions. Make sensible choices and deliver both files.
- Use only the facts in the script. Do not invent facts, quotes or statistics. Do not fabricate footage of real
  events or real people's likenesses.

Voiceover script ({client['script_language']}):
{script['voiceover'].strip()}

Video visual direction:
{script['video_prompt'].strip()}
"""