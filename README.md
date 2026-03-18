# tumblr-dl

A CLI tool for downloading media (images, videos, audio) from Tumblr blogs using the Tumblr API v2 with OAuth authentication.

## Features

- Downloads images, videos, and audio from any Tumblr blog
- **TOML config file** — define blogs, exclusions, and defaults in `~/.config/tumblr-dl/config.toml`
- **`--sync` flag** — download all configured blogs in one shot
- **Tag-based search** — search and download from any Tumblr tag (e.g. `--tag landscape`)
- **Rich metadata capture** — stores post URLs, tags, reblog trails, content labels, and timestamps in SQLite
- **Reblog trail tracking** — captures the full reblog chain from original poster to current reblogger
- **Tag exclusion** — skip posts matching glob patterns (e.g. `--exclude-tags "gore*,explicit"`)
- Extracts embedded images from text and answer posts
- Extracts video URLs from embedded iframes and NPF data attributes
- Fully async architecture for concurrent I/O
- **Incremental sync** — tracks progress in SQLite; only fetches new posts on subsequent runs
- Skips already-downloaded files (duplicate detection via DB + filesystem)
- Resumable — start from any post offset
- Paginates automatically through all blog posts
- Re-download previously failed items with `--retry-failed`
- Sanitizes filenames for cross-platform compatibility
- Prints a summary of found/downloaded/skipped/failed files by type

## Requirements

- Python 3.11+
- Tumblr API OAuth credentials ([register an app here](https://www.tumblr.com/oauth/apps))

## Installation

```bash
git clone https://github.com/nrnelson/tumblr-dl.git
cd tumblr-dl
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configuration

### Authentication

tumblr-dl loads OAuth credentials in priority order:

1. **Environment variables** (recommended):
   ```bash
   export TUMBLR_CONSUMER_KEY=your_consumer_key
   export TUMBLR_CONSUMER_SECRET=your_consumer_secret
   export TUMBLR_OAUTH_TOKEN=your_oauth_token
   export TUMBLR_OAUTH_TOKEN_SECRET=your_oauth_token_secret
   ```

   You can also use a `.env` file in the working directory (loaded automatically).

2. **TOML config file** `[auth]` section (see below).

To obtain credentials:

1. Register an application at https://www.tumblr.com/oauth/apps
2. Note the **Consumer Key** and **Consumer Secret**
3. Use the [Tumblr API console](https://api.tumblr.com/console/calls/user/info) to complete the OAuth flow and get your **OAuth Token** and **OAuth Token Secret**

### Config File

Create `~/.config/tumblr-dl/config.toml` (or set `XDG_CONFIG_HOME`):

```toml
[auth]
consumer_key = "your_consumer_key"
consumer_secret = "your_consumer_secret"
oauth_token = "your_oauth_token"
oauth_token_secret = "your_oauth_token_secret"

# App-level settings
[settings]
debug = true                    # enable debug logging + auto log file
# log_file = "~/logs/tumblr-dl.log"  # optional: explicit log file path

# Global defaults — apply to all blogs unless overridden
[defaults]
output_dir = "tumblr_downloads"
exclude_tags = ["gore*", "explicit"]
exclude_blogs = ["spambot*"]

# Per-blog sections
[blog.myblog]
output_dir = "~/media/myblog"
exclude_tags = ["nsfw"]
max_posts = 500

[blog.otherblog]
output_dir = "~/media/otherblog"
full_scan = true

[blog.tagwatch]
tag = "photography"
output_dir = "~/media/photography"
max_posts = 200
```

All per-blog keys are optional and inherit from `[defaults]`:

| Key | Type | Description |
|-----|------|-------------|
| `output_dir` | string | Directory to save media |
| `exclude_tags` | list | Glob patterns to skip by tag |
| `exclude_blogs` | list | Glob patterns to skip by reblog source |
| `max_posts` | integer | Stop after N posts |
| `start_post` | integer | Post offset to start from |
| `tag` | string | Download by tag search instead of blog |
| `full_scan` | boolean | Ignore stored cursor |
| `retry_failed` | boolean | Re-download failed items |
| `no_db` | boolean | Disable SQLite tracking |
| `db_path` | string | Custom SQLite database path |

The `[settings]` section controls app-level behavior:

| Key | Type | Description |
|-----|------|-------------|
| `debug` | boolean | Enable debug logging and auto-create a log file |
| `log_file` | string | Write debug-level logs to a specific file |

## Usage

### Ad-hoc downloads

```bash
tumblr-dl <blog_name> [blog_name ...] [options]
```

### Sync all configured blogs

```bash
tumblr-dl --sync
```

Runs all `[blog.*]` sections from your config file. CLI flags override config values.

### Arguments

| Argument | Description |
|----------|-------------|
| `blog_name` | One or more Tumblr blog names (e.g. `blog1 blog2`). Optional with `--tag`. |

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `-o`, `--output-dir DIR` | `tumblr_downloads/` | Directory to save downloaded media |
| `--config PATH` | auto-discovered | Path to TOML config file |
| `--start-post N` | `0` | Post offset to start downloading from |
| `--max-posts N` | *(all)* | Maximum number of posts to process |
| `--db-path PATH` | `<output_dir>/.tumblr-dl.db` | SQLite database location |
| `--no-db` | off | Disable SQLite tracking; use filesystem-only dedup |
| `--full-scan` | off | Ignore stored cursor; scan the entire blog |
| `--retry-failed` | off | Re-download previously failed items before main scan |
| `--tag TAG` | off | Search Tumblr by tag instead of downloading a specific blog |
| `--exclude-tags PATTERNS` | off | Comma-separated glob patterns to exclude (e.g. `nsfw,explicit*`) |
| `--exclude-blogs PATTERNS` | off | Comma-separated glob patterns of blog names to skip in reblog trails |
| `--sync` | off | Download all blogs defined in the TOML config file |
| `--debug` | off | Enable debug logging and write a log file |
| `--log-file PATH` | off | Write debug-level logs to a specific file |

CLI flags override values from the config file when explicitly provided.

### Examples

Download all media from a blog (saves to `tumblr_downloads/`):

```bash
tumblr-dl myblog
```

Download multiple blogs at once:

```bash
tumblr-dl blog1 blog2 blog3
```

Download to a custom directory:

```bash
tumblr-dl myblog -o ~/media/myblog
```

Download the first 100 posts with debug output (log file auto-created):

```bash
tumblr-dl myblog --max-posts 100 --debug
# Log written to ~/.local/state/tumblr-dl/logs/tumblr-dl-YYYYMMDD-HHMMSS.log
```

Sync all configured blogs from config.toml:

```bash
tumblr-dl --sync
```

Sync with a CLI override:

```bash
tumblr-dl --sync --max-posts 50
```

Re-run and only fetch new posts (automatic — just run the same command again):

```bash
tumblr-dl myblog  # first run: full scan, builds DB
tumblr-dl myblog  # second run: stops at last-seen post
```

Force a full re-scan ignoring the stored cursor:

```bash
tumblr-dl myblog --full-scan
```

Retry previously failed downloads:

```bash
tumblr-dl myblog --retry-failed
```

Search by tag across all of Tumblr:

```bash
tumblr-dl --tag landscape --max-posts 200
```

> **Known limitation:** The `--tag` option uses Tumblr's `/v2/tagged` API endpoint,
> which is a legacy endpoint that returns **incomplete results** compared to the web
> interface at `tumblr.com/tagged/<tag>`. For example, a tag like `filmphotography`
> may show 50+ posts when scrolling on the website but only return ~20 via the API.
> This is a confirmed server-side limitation
> ([tumblr/docs#136](https://github.com/tumblr/docs/issues/136),
> [tumblr/docs#77](https://github.com/tumblr/docs/issues/77)):
>
> - The API only indexes posts where the tag appears in the **first 5 tags** (the web
>   UI indexes the first 20).
> - The endpoint applies undocumented spam/quality filtering.
> - Timestamp-based pagination is unreliable because the underlying tag index doesn't
>   store publish timestamps
>   ([tumblr/docs#131](https://github.com/tumblr/docs/issues/131)).
>
> There are no query parameters that can work around this. For complete results from
> a **specific blog**, use `tumblr-dl blogname` instead — the blog endpoint
> exhaustively paginates all posts.

Download a blog but skip posts with certain tags:

```bash
tumblr-dl myblog --exclude-tags "gore*,explicit,minors"
```

Skip posts reblogged from specific blogs:

```bash
tumblr-dl myblog --exclude-blogs "spambot*,unwantedblog"
```

Combine tag search with tag and blog exclusion:

```bash
tumblr-dl --tag photography --exclude-tags "ai*,generated" --exclude-blogs "spambot*" --max-posts 500
```

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `2` | Configuration error (missing credentials or invalid config) |
| `3` | Runtime error (API failure) |

## Supported Post Types

| Post Type | What Gets Downloaded |
|-----------|---------------------|
| Photo | Original-size images from photo posts |
| Video | Direct video URLs, embedded iframes, and NPF video attributes |
| Audio | Direct audio URLs |
| Text | Embedded `<img>` tags and NPF video figures in post body |
| Answer | Embedded `<img>` tags and NPF video figures in answer body |

## Architecture

### Native async with `curl_cffi`

This project uses a fully async (`asyncio`) architecture with
[curl_cffi](https://github.com/lexiforest/curl_cffi) for HTTP. curl_cffi provides
native async support via `AsyncSession` and uses libcurl under the hood, whose TLS
stack is accepted by Tumblr's CDN fingerprinting (unlike pure-Python clients like
`httpx` and `aiohttp`, which get HTTP 403 from Tumblr's nginx layer).

OAuth1 request signing is handled by `oauthlib` directly.

### Incremental sync with SQLite

On first run, tumblr-dl scans the entire blog and stores the highest post ID in a
SQLite database (`<output_dir>/.tumblr-dl.db`). On subsequent runs, it fetches posts
newest-first and **stops as soon as it hits a previously-seen post ID**. This avoids
re-fetching the entire blog via the API each time — only new posts are processed.

The database also tracks individual file downloads (URL, status, file size), enabling
`--retry-failed` to re-attempt only previously failed downloads. Use `--no-db` to
disable tracking entirely, or `--full-scan` to ignore the stored cursor for one run.

### Rich metadata in SQLite

Beyond download tracking, the database captures post metadata for later querying:

- **Post tags** — stored in `post_tags` table, normalized to lowercase
- **Reblog trail** — full reblog chain in `reblog_trail` table (original poster through each reblogger)
- **Timestamps** — both the reblog and original post timestamps
- **Content labels** — Tumblr Community Labels (Mature, Sexual Themes, etc.)
- **Post URLs** — canonical Tumblr URLs for each downloaded media item
- **Skipped posts** — posts excluded by `--exclude-tags` with the reason and matched tag

Example queries against the database:

```sql
-- Find original posters for content discovered on a blog
SELECT DISTINCT trail_blog_name, COUNT(*) as posts
FROM reblog_trail WHERE blog_name = 'someblog' AND is_root = 1
GROUP BY trail_blog_name ORDER BY posts DESC;

-- Find all posts with a specific tag
SELECT DISTINCT blog_name, post_id FROM post_tags WHERE tag = 'landscape';

-- See what was excluded and why
SELECT * FROM skipped_posts WHERE blog_name = 'someblog';
```

### Why not `httpx`?

Tumblr's API and CDN use TLS fingerprinting (JA3/JA4) to block non-browser clients.
Python's `ssl` module — used by `httpx` and `aiohttp` — produces a recognizable
TLS ClientHello that Tumblr rejects with 403. There is no way to customize cipher
ordering, TLS extension ordering, or other fingerprint parameters through `httpx`,
even with custom transports or SSL contexts.

### Why not `pytumblr`?

The official `pytumblr` library was replaced with a direct Tumblr API v2 client
because pytumblr is synchronous-only, and we only need the `GET /posts` endpoint.
Our client is ~160 lines vs pytumblr's ~770, with full type safety and async support.

## Development

```bash
# Install with dev dependencies
source .venv/bin/activate
pip install -e ".[dev]"

# Format
ruff format src/ tests/

# Lint
ruff check src/ tests/

# Type check
mypy src/

# Run all checks
ruff format --check src/ tests/ && ruff check src/ tests/ && mypy src/

# Test
pytest tests/
pytest tests/ --cov --cov-report=term-missing
```

## Project Structure

```
src/tumblr_dl/
├── __init__.py        — package + version
├── cli.py             — async argparse + main() entry point
├── client.py          — async TumblrClient (OAuth1 + Tumblr API v2)
├── config.py          — TOML config + env var auth loading
├── downloader.py      — async download_item() with DedupStrategy ABC
├── exceptions.py      — TumblrDlError hierarchy
├── extractors.py      — media URL extraction per post type
├── models.py          — enums (DownloadStatus, MediaType) + dataclasses
├── tracker.py         — SQLite-based download tracker for incremental sync
└── utils.py           — sanitize_filename
```

## License

MIT
