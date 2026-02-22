"""Zero-auth X/Twitter search, rank, and digest pipeline."""

import argparse
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
import warnings
from datetime import datetime
from pathlib import Path

import requests
import yaml
from ddgs import DDGS

DEFAULT_CONFIG_PATH = Path("~/.config/trawlx/config.yaml").expanduser()

log = logging.getLogger("trawlx")
logging.getLogger("ddgs").setLevel(logging.WARNING)
logging.getLogger("primp").setLevel(logging.WARNING)


def setup_logging():
    """Configure logging to file and stderr."""
    log_dir = Path("~/.config/trawlx/logs").expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(log_dir / "trawlx.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)


def load_config(path: Path) -> dict:
    """Load YAML config from path. Returns {} if file not found."""
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def search_tweets(query: str, num: int) -> list[str]:
    """Search DuckDuckGo for X/Twitter URLs matching the query."""
    full_query = f"site:x.com {query}"
    urls = []
    try:
        with warnings.catch_warnings(), open(os.devnull, "w") as devnull, contextlib.redirect_stderr(devnull):
            warnings.simplefilter("ignore")
            results = DDGS().text(full_query, max_results=num, timelimit="d")
        for r in results:
            href = r.get("href", "")
            if "x.com" in href and "/status/" in href:
                urls.append(href)
    except Exception as e:
        log.error(f"Search failed: {e}")
    log.info(f"Found {len(urls)} tweet URLs for query: {query[:60]}...")
    return urls


def extract_tweet_id(url: str) -> str | None:
    """Pull the tweet ID from an x.com or twitter.com URL."""
    m = re.search(r"(?:x\.com|twitter\.com)/\w+/status/(\d+)", url)
    return m.group(1) if m else None


def extract_username(url: str) -> str | None:
    """Pull the username from a tweet URL."""
    m = re.search(r"(?:x\.com|twitter\.com)/(\w+)/status/", url)
    return m.group(1) if m else None


def fetch_tweet(url: str) -> dict | None:
    """Fetch tweet data from fxtwitter API."""
    username = extract_username(url)
    tweet_id = extract_tweet_id(url)
    if not tweet_id or not username:
        return None

    api_url = f"https://api.fxtwitter.com/{username}/status/{tweet_id}"
    try:
        resp = requests.get(api_url, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        tweet = data.get("tweet")
        if not tweet:
            return None
        author = tweet.get("author") or {}
        return {
            "id": tweet_id,
            "url": url,
            "author": author.get("screen_name", username),
            "author_name": author.get("name", ""),
            "text": tweet.get("text", ""),
            "likes": tweet.get("likes", 0),
            "retweets": tweet.get("retweets", 0),
            "replies": tweet.get("replies", 0),
            "created_at": tweet.get("created_at", ""),
        }
    except Exception as e:
        log.warning(f"Failed to fetch tweet {tweet_id}: {e}")
        return None


def fetch_replies(tweet_id: str, username: str) -> list[dict]:
    """Fetch thread/conversation content for a tweet via fxtwitter."""
    api_url = f"https://api.fxtwitter.com/{username}/status/{tweet_id}"
    try:
        resp = requests.get(api_url, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        tweet = data.get("tweet") or {}
        thread = tweet.get("thread") or []
        replies = []
        for reply in thread:
            author = reply.get("author") or {}
            replies.append({
                "author": author.get("screen_name", ""),
                "text": reply.get("text", ""),
            })
        return replies
    except Exception:
        return []


def build_claude_prompt(tweets: list[dict]) -> str:
    """Format tweets into a prompt for Claude summarization."""
    lines = []
    for i, t in enumerate(tweets, 1):
        engagement = f"[{t['likes']}L/{t['retweets']}RT/{t['replies']}R]"
        lines.append(f"---\nTweet {i} by @{t['author']} ({t['author_name']}) {engagement}\nURL: {t['url']}\nPosted: {t['created_at']}")
        lines.append(t["text"])
        if t.get("reply_texts"):
            lines.append("Notable replies:")
            for r in t["reply_texts"][:5]:
                lines.append(f"  @{r['author']}: {r['text']}")
    tweet_block = "\n".join(lines)

    return f"""You are producing a daily digest of X/Twitter posts about Claude, AI coding tools, MCP, and related topics.

Below are {len(tweets)} tweets collected today, ranked by engagement. Analyze them and produce a structured markdown report.

## Output format

Use these sections (skip any section with no relevant content):

### Key Announcements
Major product launches, updates, or official news.

### Tips & Workflows
Practical tips, prompts, config snippets, or workflow advice.

### Tools & Integrations
New MCP servers, extensions, integrations, or open-source projects.

### Community Discussion
Interesting debates, opinions, or threads.

### Notable Threads
Longer threads worth reading in full (link to them).

For each item:
- One-line summary of the insight
- Link to the original tweet (use the EXACT URL provided for each tweet, do NOT fabricate URLs)
- @author attribution and post date/time

Keep it concise and scannable. No fluff. IMPORTANT: Use the exact tweet URLs provided — never generate or guess URLs.

---
TWEETS:
{tweet_block}"""


def summarize(prompt: str) -> str:
    """Call claude -p to summarize the tweets."""
    try:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        if result.returncode != 0:
            log.error(f"claude -p failed: {result.stderr}")
            return ""
        return result.stdout.strip()
    except FileNotFoundError:
        log.error("'claude' CLI not found in PATH")
        return ""
    except subprocess.TimeoutExpired:
        log.error("claude -p timed out after 120s")
        return ""


# --- Output formatters ---


def format_json(tweets: list[dict], **_) -> None:
    """Dump tweets as JSON to stdout."""
    print(json.dumps(tweets, indent=2, ensure_ascii=False))


def format_markdown(tweets: list[dict], date: str, output_dir: Path, output_file: Path | None = None, **_) -> None:
    """Write a structured markdown digest without AI summarization."""
    lines = [
        f"# X/Twitter Digest — {date}\n",
        f"*{len(tweets)} posts, ranked by engagement*\n",
    ]
    for i, t in enumerate(tweets, 1):
        engagement = f"{t['likes']}L / {t['retweets']}RT / {t['replies']}R"
        lines.append(f"## {i}. @{t['author']} ({t['author_name']})")
        lines.append(f"")
        quoted = "\n> ".join(t["text"].splitlines())
        lines.append(f"> {quoted}")
        lines.append(f"")
        lines.append(f"**Engagement:** {engagement}  ")
        lines.append(f"**Posted:** {t['created_at']}  ")
        lines.append(f"**Link:** {t['url']}")
        if t.get("reply_texts"):
            lines.append("")
            lines.append("<details><summary>Thread replies</summary>\n")
            for r in t["reply_texts"][:5]:
                lines.append(f"> **@{r['author']}:** {r['text']}")
                lines.append("")
            lines.append("</details>")
        lines.append("")

    if not output_file:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"{date}.md"
    else:
        output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("\n".join(lines) + "\n")
    log.info(f"Digest written to {output_file}")
    print(f"Digest saved: {output_file}")


def format_claude(tweets: list[dict], date: str, output_dir: Path, output_file: Path | None = None, **_) -> None:
    """Summarize tweets with Claude and write markdown output."""
    prompt = build_claude_prompt(tweets)
    log.info(f"Sending {len(tweets)} tweets to claude for summarization...")
    summary = summarize(prompt)

    if not summary:
        log.error("Summarization returned empty. Aborting.")
        return

    header = f"# AI/Claude Daily Digest — {date}\n\n"
    header += f"*Generated from {len(tweets)} top posts on X/Twitter*\n\n"

    if not output_file:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"{date}.md"
    else:
        output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(header + summary + "\n")
    log.info(f"Digest written to {output_file}")
    print(f"Digest saved: {output_file}")


# --- Launchd management ---


def install_launchd(hour: int = 8, minute: int = 0):
    """Generate and load a launchd plist for daily trawlx runs."""
    trawlx_bin = shutil.which("trawlx")
    if not trawlx_bin:
        print("Error: 'trawlx' not found in PATH. Install it first (pipx install .).")
        return

    bin_dir = str(Path(trawlx_bin).parent)
    home = str(Path.home())
    uid = os.getuid()
    log_path = f"{home}/.config/trawlx/logs/launchd.log"

    # Build PATH: trawlx bin dir + homebrew + standard + nvm node (for claude CLI)
    nvm_dir = os.environ.get("NVM_DIR", f"{home}/.nvm")
    node_bin = ""
    nvm_default = Path(nvm_dir) / "alias" / "default"
    if nvm_default.exists():
        version = nvm_default.read_text().strip()
        candidate = Path(nvm_dir) / "versions" / "node" / f"v{version}" / "bin"
        if not candidate.exists():
            # try glob for partial match
            for p in sorted((Path(nvm_dir) / "versions" / "node").glob(f"v{version}*")):
                candidate = p / "bin"
                break
        if candidate.exists():
            node_bin = f":{candidate}"

    env_path = f"{bin_dir}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin{node_bin}"

    plist_content = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>dev.timstark.trawlx</string>

            <key>ProgramArguments</key>
            <array>
                <string>{trawlx_bin}</string>
                <string>--mode</string>
                <string>claude</string>
            </array>

            <key>EnvironmentVariables</key>
            <dict>
                <key>PATH</key>
                <string>{env_path}</string>
            </dict>

            <key>StartCalendarInterval</key>
            <dict>
                <key>Hour</key>
                <integer>{hour}</integer>
                <key>Minute</key>
                <integer>{minute}</integer>
            </dict>

            <key>StandardOutPath</key>
            <string>{log_path}</string>
            <key>StandardErrorPath</key>
            <string>{log_path}</string>
        </dict>
        </plist>
    """)

    # Ensure log dir exists
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)

    # Remove old plist if it exists
    old_plist = Path(home) / "Library/LaunchAgents/dev.timstark.daily-digest.plist"
    if old_plist.exists():
        print(f"Found old plist: {old_plist}")
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", str(old_plist)],
            capture_output=True,
        )
        old_plist.unlink()
        print("  Unloaded and removed old plist.")

    plist_path = Path(home) / "Library/LaunchAgents/dev.timstark.trawlx.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # Unload existing if present
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
            capture_output=True,
        )

    plist_path.write_text(plist_content)
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        check=True,
    )

    print(f"Installed launchd job:")
    print(f"  Binary:  {trawlx_bin}")
    print(f"  PATH:    {env_path}")
    print(f"  Plist:   {plist_path}")
    print(f"  Log:     {log_path}")
    print(f"  Schedule: daily at {hour:02d}:{minute:02d}")


def uninstall_launchd():
    """Unload and remove the trawlx launchd plist."""
    home = str(Path.home())
    uid = os.getuid()
    plist_path = Path(home) / "Library/LaunchAgents/dev.timstark.trawlx.plist"

    if not plist_path.exists():
        print("No launchd plist found. Nothing to uninstall.")
        return

    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
        capture_output=True,
    )
    plist_path.unlink()
    print(f"Uninstalled: removed {plist_path}")


# --- Config management ---


def setup(config_path: Path):
    """Interactive first-run setup."""
    print("trawlx setup\n")

    # Output directory
    default_dir = "~/Documents/trawlx"
    output_dir = input(f"Where should digests be saved? [{default_dir}]: ").strip()
    output_dir = output_dir or default_dir

    # Daily or manual?
    daily = input("\nSet up a daily recurring digest? [Y/n]: ").strip().lower()

    if daily in ("", "y", "yes"):
        # Full walkthrough: queries, min likes, schedule time
        print("\nAdd search queries (DuckDuckGo syntax, searches x.com automatically).")
        print("Examples:")
        print('  "claude code" OR "cursor ai"')
        print('  ("react" OR "next.js") framework')
        print("")
        queries = []
        while True:
            prompt = "Query: " if not queries else "Another query (enter to finish): "
            q = input(prompt).strip()
            if not q:
                if not queries:
                    print("Need at least one query.")
                    continue
                break
            queries.append(q)

        default_likes = "3"
        min_likes_input = input(f"\nMinimum likes to include a tweet? [{default_likes}]: ").strip()
        min_likes = int(min_likes_input) if min_likes_input else int(default_likes)

        default_time = "08:00"
        time_input = input(f"What time should it run? (24h format) [{default_time}]: ").strip()
        time_str = time_input or default_time
        try:
            hour, minute = int(time_str.split(":")[0]), int(time_str.split(":")[1])
        except (ValueError, IndexError):
            print(f"Invalid time format, using {default_time}")
            hour, minute = 8, 0

        config = {
            "queries": queries,
            "max_results_per_query": 20,
            "max_tweets_to_summarize": 30,
            "min_likes": min_likes,
            "fetch_replies": True,
            "mode": "markdown",
            "output_dir": output_dir,
        }

        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        Path(os.path.expanduser(output_dir)).mkdir(parents=True, exist_ok=True)

        print(f"\nConfig saved to {config_path}")
        install_launchd(hour=hour, minute=minute)
        print(f"\nRun `trawlx` now to generate your first digest.")
    else:
        # Minimal config, manual usage
        config = {
            "queries": [],
            "max_results_per_query": 20,
            "max_tweets_to_summarize": 30,
            "min_likes": 3,
            "fetch_replies": True,
            "mode": "markdown",
            "output_dir": output_dir,
        }

        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        Path(os.path.expanduser(output_dir)).mkdir(parents=True, exist_ok=True)

        print(f"\nConfig saved to {config_path}")
        print(f"\nRun a digest:")
        print(f'  trawlx --query "claude code"')
        print(f"\nSave a query for reuse:")
        print(f'  trawlx --query "claude code" --save')


def save_query(query: str, config_path: Path, config: dict):
    """Append a query to the config file."""
    queries = config.get("queries", [])
    if query in queries:
        print(f"Query already saved: {query}")
        return

    config.setdefault("queries", []).append(query)
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    print(f"Saved query to {config_path}: {query}")


# --- Main ---


def main():
    parser = argparse.ArgumentParser(description="Zero-auth X/Twitter search, rank, and digest pipeline")
    parser.add_argument("--query", help="One-off custom search query")
    parser.add_argument("--save", action="store_true", help="Save --query to config for future runs")
    parser.add_argument("--date", help="Override output date (YYYY-MM-DD)")
    parser.add_argument("--mode", choices=["json", "markdown", "claude"], help="Output mode")
    parser.add_argument("--output", "-o", help="Output filename (saved to digests dir, .md added if missing)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to config file")
    parser.add_argument("--setup", action="store_true", help="Interactive first-run setup")
    parser.add_argument("--install-launchd", action="store_true", help="Install launchd plist for daily runs")
    parser.add_argument("--uninstall-launchd", action="store_true", help="Remove launchd plist")
    args = parser.parse_args()

    if args.setup:
        setup(args.config)
        return
    if args.install_launchd:
        config = load_config(args.config)
        if not config.get("queries"):
            print("No queries configured yet.\n")
            print("Add search queries (DuckDuckGo syntax, searches x.com automatically).")
            print("Examples:")
            print('  "claude code" OR "cursor ai"')
            print('  ("react" OR "next.js") framework')
            print("")
            queries = []
            while True:
                prompt = "Query: " if not queries else "Another query (enter to finish): "
                q = input(prompt).strip()
                if not q:
                    if not queries:
                        print("Need at least one query.")
                        continue
                    break
                queries.append(q)
            config.setdefault("queries", []).extend(queries)
            args.config.parent.mkdir(parents=True, exist_ok=True)
            with open(args.config, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            print(f"Queries saved to {args.config}\n")

        default_time = "08:00"
        time_input = input(f"What time should it run? (24h format) [{default_time}]: ").strip()
        time_str = time_input or default_time
        try:
            hour, minute = int(time_str.split(":")[0]), int(time_str.split(":")[1])
        except (ValueError, IndexError):
            print(f"Invalid time format, using {default_time}")
            hour, minute = 8, 0
        install_launchd(hour=hour, minute=minute)
        return
    if args.uninstall_launchd:
        uninstall_launchd()
        return

    setup_logging()

    config = load_config(args.config)

    # --save requires --query and existing config
    if args.save:
        if not args.query:
            print("--save requires --query")
            return
        if not config:
            print("No config found. Run `trawlx --setup` first.")
            return
        save_query(args.query, args.config, config)
        config = load_config(args.config)

    # No config and no --query: run setup
    if not config and not args.query:
        setup(args.config)
        return

    today = args.date or datetime.now().strftime("%Y-%m-%d")
    mode = args.mode or config.get("mode", "markdown")
    output_dir = Path(os.path.expanduser(config.get("output_dir", "~/Documents/trawlx")))
    queries = [args.query] if args.query else config.get("queries", [])
    max_results = config.get("max_results_per_query", 20)
    max_tweets = config.get("max_tweets_to_summarize", 30)
    min_likes = config.get("min_likes", 3)
    do_replies = config.get("fetch_replies", True)

    # 1. Search for tweet URLs
    log.info(f"Starting digest for {today}")
    all_urls = []
    for q in queries:
        all_urls.extend(search_tweets(q, max_results))

    # Deduplicate by tweet ID
    seen_ids = set()
    unique_urls = []
    for url in all_urls:
        tid = extract_tweet_id(url)
        if tid and tid not in seen_ids:
            seen_ids.add(tid)
            unique_urls.append(url)
    log.info(f"Unique tweet URLs: {len(unique_urls)}")

    # 2. Fetch tweet content
    tweets = []
    for url in unique_urls:
        tweet = fetch_tweet(url)
        if tweet:
            tweets.append(tweet)

    log.info(f"Fetched {len(tweets)} tweets successfully")

    # 3. Filter by minimum engagement
    tweets = [t for t in tweets if t["likes"] >= min_likes]
    log.info(f"After filtering (min {min_likes} likes): {len(tweets)} tweets")

    if not tweets:
        log.warning("No tweets passed filters. Skipping digest.")
        return

    # 4. Sort by engagement (likes + retweets)
    tweets.sort(key=lambda t: t["likes"] + t["retweets"], reverse=True)
    tweets = tweets[:max_tweets]

    # 5. Fetch replies for top posts
    if do_replies:
        for t in tweets[:10]:
            replies = fetch_replies(t["id"], t["author"])
            if replies:
                t["reply_texts"] = replies
                log.info(f"Fetched {len(replies)} replies for tweet {t['id']}")

    # 6. Output
    formatters = {
        "json": format_json,
        "markdown": format_markdown,
        "claude": format_claude,
    }
    output_file = None
    if args.output:
        name = args.output if args.output.endswith(".md") else f"{args.output}.md"
        output_file = output_dir / name
    formatters[mode](tweets=tweets, date=today, output_dir=output_dir, output_file=output_file)


if __name__ == "__main__":
    main()
