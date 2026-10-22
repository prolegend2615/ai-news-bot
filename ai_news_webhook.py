"""
AI News -> Discord webhook.

Setup:
    pip install feedparser requests
    set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/XXXX/YYYY   (Windows cmd)
    $env:DISCORD_WEBHOOK_URL="..."                                       (PowerShell)
    python ai_news_webhook.py

Run it every 15-30 minutes (Task Scheduler, or GitHub Actions - see ai-news.yml).
First run only "seeds" the seen list so you don't get 200 posts at once.
Use  --post-initial 5  to post the newest 5 items on first run instead.
"""
import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import feedparser
import requests

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
SEEN_FILE = Path(__file__).with_name("seen.json")
MAX_POSTS_PER_RUN = 15

# (name, url, color, needs_keyword_filter)
# Dedicated AI sources post everything; general tech/security sources are filtered by keyword.
FEEDS = [
    # --- AI labs & model news ---
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml", 0xFFD21E, False),
    ("OpenAI", "https://openai.com/news/rss.xml", 0x10A37F, False),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml", 0x4285F4, False),
    ("Google AI Blog", "https://blog.google/technology/ai/rss/", 0x4285F4, False),
    ("NVIDIA Blog (AI)", "https://blogs.nvidia.com/blog/category/deep-learning/feed/", 0x76B900, False),
    # --- AI news media ---
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", 0x0A9E01, False),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", 0xFA4B2A, False),
    ("MIT Tech Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed", 0xD0021B, False),
    # --- Community / trending ---
    ("Hacker News (AI)", "https://hnrss.org/newest?q=LLM+OR+%22AI+model%22+OR+OpenAI+OR+Anthropic+OR+Gemini&points=150", 0xFF6600, False),
    # --- Security / incidents (filtered: only AI-related) ---
    ("BleepingComputer", "https://www.bleepingcomputer.com/feed/", 0xE23D28, True),
    ("The Hacker News", "https://feeds.feedburner.com/TheHackersNews", 0x1F2933, True),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", 0xFF4E00, True),
]

# Add or remove words to tune what counts as "AI news" for filtered feeds.
KEYWORDS = re.compile(
    r"\b(ai|a\.i\.|llm|llms|gpt|chatgpt|openai|anthropic|claude|gemini|deepmind|"
    r"hugging ?face|mistral|llama|deepseek|qwen|grok|xai|copilot|diffusion|"
    r"machine learning|neural|model weights|prompt injection|jailbreak|"
    r"agentic|ai agent|generative)\b",
    re.IGNORECASE,
)


def clean(text: str, limit: int) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def load_seen() -> tuple[set[str], bool]:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8"))), False
    return set(), True


def save_seen(seen: set[str]) -> None:
    # keep the file from growing forever
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-5000:]), encoding="utf-8")


def entry_time(e) -> float:
    t = e.get("published_parsed") or e.get("updated_parsed")
    return time.mktime(t) if t else 0.0


def fetch_new(seen: set[str]) -> list[dict]:
    items = []
    for name, url, color, filtered in FEEDS:
        try:
            feed = feedparser.parse(url, agent="ai-news-webhook/1.0")
        except Exception as ex:  # one bad feed shouldn't kill the run
            print(f"[warn] {name}: {ex}", file=sys.stderr)
            continue
        for e in feed.entries[:25]:
            uid = e.get("id") or e.get("link")
            if not uid or uid in seen:
                continue
            title = clean(e.get("title", ""), 250)
            summary = clean(e.get("summary", ""), 350)
            if filtered and not KEYWORDS.search(f"{title} {summary}"):
                continue
            items.append(
                {
                    "uid": uid,
                    "source": name,
                    "color": color,
                    "title": title,
                    "summary": summary,
                    "link": e.get("link", ""),
                    "ts": entry_time(e),
                }
            )
    items.sort(key=lambda i: i["ts"])  # oldest first so the channel reads chronologically
    return items


def post(item: dict) -> bool:
    embed = {
        "title": item["title"],
        "url": item["link"],
        "description": item["summary"],
        "color": item["color"],
        "footer": {"text": item["source"]},
    }
    if item["ts"]:
        embed["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(item["ts"]))
    for _ in range(3):
        r = requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
        if r.status_code == 429:  # rate limited
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        if r.ok:
            return True
        print(f"[error] Discord {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return False
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post-initial", type=int, default=0,
                    help="on the very first run, post this many newest items instead of staying silent")
    ap.add_argument("--dry-run", action="store_true", help="print instead of posting")
    args = ap.parse_args()

    if not WEBHOOK_URL and not args.dry_run:
        sys.exit("Set DISCORD_WEBHOOK_URL first.")

    seen, first_run = load_seen()
    items = fetch_new(seen)
    print(f"{len(items)} new items found")

    if first_run:
        to_post = items[-args.post_initial:] if args.post_initial else []
        seen.update(i["uid"] for i in items)  # mark everything as seen
        items = to_post
    else:
        items = items[-MAX_POSTS_PER_RUN:]
        # anything beyond the cap is dropped from this run; mark all as seen to avoid a backlog flood
        seen.update(i["uid"] for i in fetch_new(seen))

    for item in items:
        if args.dry_run:
            print(f"[{item['source']}] {item['title']}\n  {item['link']}")
        elif post(item):
            seen.add(item["uid"])
            time.sleep(1.5)  # stay well under Discord's webhook rate limit

    if not args.dry_run:
        save_seen(seen)


if __name__ == "__main__":
    main()
