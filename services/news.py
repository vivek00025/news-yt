"""Fetches the latest political headlines for a client from Google News RSS
(plus any custom RSS feeds the client adds). No API key required."""
import hashlib
import html
import re
from calendar import timegm
from datetime import datetime, timezone
from urllib.parse import quote_plus

import feedparser
import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsreelBot/1.0)"}


class NoNewsError(RuntimeError):
    pass


def google_news_url(query: str, country: str, language: str, freshness: str = "1d") -> str:
    country = (country or "US").upper()
    language = (language or "en").lower()
    q = quote_plus(f"{query} when:{freshness}")
    return (f"https://news.google.com/rss/search?q={q}"
            f"&hl={language}-{country}&gl={country}&ceid={country}:{language}")


def _strip_source(title: str) -> tuple[str, str]:
    """Google News titles look like 'Headline text - Publisher'."""
    if " - " in title:
        head, _, tail = title.rpartition(" - ")
        if head and len(tail) < 60:
            return head.strip(), tail.strip()
    return title.strip(), ""


def article_hash(title: str) -> str:
    norm = re.sub(r"\W+", " ", title.lower()).strip()
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def _clean(text: str, limit=300) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()[:limit]


def _parse(url: str):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return feedparser.parse(r.content)


def entries_from_feed(feed, feed_name=""):
    out = []
    for e in feed.entries[:50]:
        raw_title = _clean(e.get("title", ""), 300)
        if not raw_title:
            continue
        head, src = _strip_source(raw_title)
        src = (e.get("source", {}) or {}).get("title") or src or feed_name
        published = None
        if e.get("published_parsed"):
            published = datetime.fromtimestamp(timegm(e.published_parsed), tz=timezone.utc)
        summary = _clean(e.get("summary", ""))
        if summary.lower().startswith(head.lower()[:40]):
            summary = ""  # Google News summaries often just repeat the title
        out.append({"title": head, "source": src, "link": e.get("link", ""),
                    "published": published, "summary": summary, "hash": article_hash(head)})
    return out


def fetch_headlines(client, exclude_hashes: set, limit: int | None = None, log=print) -> list[dict]:
    limit = limit or client["headlines_count"]
    google = {f: google_news_url(client["news_query"], client["country"], client["language"], f)
              for f in ("1d", "3d")}
    extra = [u.strip() for u in (client["extra_feeds"] or "").splitlines() if u.strip().startswith("http")]

    def collect(feed_urls):
        items, seen = [], set(exclude_hashes)
        for url in feed_urls:
            try:
                for it in entries_from_feed(_parse(url)):
                    if it["hash"] not in seen:
                        seen.add(it["hash"])
                        items.append(it)
            except Exception as exc:  # one bad feed shouldn't sink the run
                log(f"Feed failed ({url[:70]}...): {exc}")
        floor = datetime.min.replace(tzinfo=timezone.utc)
        items.sort(key=lambda i: i["published"] or floor, reverse=True)
        return items

    items = collect([google["1d"]] + extra)
    if len(items) < 3:                      # quiet news day: widen the window
        items = collect([google["3d"]] + extra)
    if not items:
        raise NoNewsError("No unused headlines found for this client's query.")
    return items[:limit]
