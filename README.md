# trawlx

A simple Python pipeline that gets structured Twitter data — text, likes, retweets, threads — without authentication. DuckDuckGo finds the tweets, fxtwitter returns the JSON, you do what you want with it.

## Install

```bash
pipx install ~/code/trawlx
```

## Setup

Run `trawlx` — setup runs automatically on first launch:

```
$ trawlx

trawlx setup

Where should digests be saved? [~/Documents/trawlx]:

Set up a daily recurring digest? [Y/n]:
```

**If yes** — walks you through queries, min likes, and schedule time, then installs a launchd job:

```
Add search queries (DuckDuckGo syntax, searches x.com automatically).
Examples:
  "claude code" OR "cursor ai"
  ("react" OR "next.js") framework

Query: "claude code" OR mcp
Another query (enter to finish):

Minimum likes to include a tweet? [3]:

What time should it run? (24h format) [08:00]: 07:30

Config saved to ~/.config/trawlx/config.yaml
Installed launchd job:
  Schedule: daily at 07:30

Run `trawlx` now to generate your first digest.
```

**If no** — saves minimal config and shows how to run manually:

```
Config saved to ~/.config/trawlx/config.yaml

Run a digest:
  trawlx --query "claude code"

Save a query for reuse:
  trawlx --query "claude code" --save
```

Re-run setup anytime with `trawlx --setup`.

## Usage

```bash
# Run with saved queries
trawlx

# One-off query
trawlx --query "claude code"

# Save a query to config
trawlx --query "claude code" --save

# Custom output filename (saved to digests dir)
trawlx --query "agentic coding" -o agentic

# AI-summarized digest via claude CLI
trawlx --mode claude

# Raw JSON to stdout
trawlx --mode json
```

## Output Modes

| Mode | Output | Requires |
|---|---|---|
| `json` | Tweet objects as JSON to stdout | Nothing |
| `markdown` | Structured digest to `{output_dir}/{date}.md` | Nothing |
| `claude` | AI-summarized digest to `{output_dir}/{date}.md` | `claude` CLI in PATH |

## Scheduled Runs (macOS)

Setup offers to configure this automatically. To set up later:

```bash
# Walks you through queries (if needed) and schedule time
trawlx --install-launchd

# Remove the scheduled job
trawlx --uninstall-launchd
```

## Config

Config lives at `~/.config/trawlx/config.yaml`. Queries use DuckDuckGo syntax with `site:x.com` prepended automatically. Results are limited to the last 24 hours.

Logs go to `~/.config/trawlx/logs/`.
