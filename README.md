# tumblr-dl

A CLI tool for downloading media (images, videos, audio) from Tumblr blogs using the Tumblr API v2 with OAuth authentication.

## Features

- Downloads images, videos, and audio from any Tumblr blog
- Extracts embedded images from text and answer posts
- Extracts video URLs from embedded iframes and NPF data attributes
- Fully async architecture for concurrent I/O
- Skips already-downloaded files (duplicate detection)
- Resumable — start from any post offset
- Paginates automatically through all blog posts
- Sanitizes filenames for cross-platform compatibility
- Prints a summary of found/downloaded/skipped/failed files by type

## Requirements

- Python 3.10+
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

Create a `~/.tumblr` file with your Tumblr API OAuth credentials in YAML format:

```yaml
consumer_key: your_consumer_key
consumer_secret: your_consumer_secret
oauth_token: your_oauth_token
oauth_token_secret: your_oauth_token_secret
```

To obtain these credentials:

1. Register an application at https://www.tumblr.com/oauth/apps
2. Note the **Consumer Key** and **Consumer Secret**
3. Use the [Tumblr API console](https://api.tumblr.com/console/calls/user/info) to complete the OAuth flow and get your **OAuth Token** and **OAuth Token Secret**

## Usage

```bash
tumblr-dl <blog_name> <output_dir> [options]
```

### Arguments

| Argument | Description |
|----------|-------------|
| `blog_name` | The Tumblr blog name (e.g. `example` for example.tumblr.com) |
| `output_dir` | Directory to save downloaded media |

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--config PATH` | `~/.tumblr` | Path to YAML OAuth config file |
| `--start-post N` | `0` | Post offset to start downloading from |
| `--max-posts N` | *(all)* | Maximum number of posts to process |
| `--debug` | off | Enable debug logging |

### Examples

Download all media from a blog:

```bash
tumblr-dl myblog ./downloads
```

Download the first 100 posts with debug output:

```bash
tumblr-dl myblog ./downloads --max-posts 100 --debug
```

Resume from post offset 200 with a custom config file:

```bash
tumblr-dl myblog ./downloads --start-post 200 --config ~/my-creds.yaml
```

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `2` | Configuration error (missing config file or keys) |
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

### Async with `requests` via thread pool

This project uses an async (`asyncio`) architecture, but delegates HTTP calls to
`requests`/`requests-oauthlib` running in thread pools via `asyncio.to_thread()`.

This is intentional: Tumblr's API and CDN employ TLS fingerprinting that rejects
connections from pure-Python HTTP clients like `httpx` and `aiohttp` (both return
HTTP 403 from Tumblr's nginx layer regardless of valid OAuth credentials). The
`requests` library uses `urllib3`, whose TLS stack is accepted by Tumblr.

The async wrapper still provides:
- Non-blocking pagination delays (`asyncio.sleep`)
- Foundation for concurrent downloads in future versions
- Clean async context manager lifecycle for the API client

### Why not `pytumblr`?

The official `pytumblr` library was replaced with a direct Tumblr API v2 client
because pytumblr is synchronous-only, and we only need the `GET /posts` endpoint.
Our client is ~170 lines vs pytumblr's ~770, with full type safety and async support.

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
├── downloader.py      — async download_item() with DedupStrategy ABC
├── exceptions.py      — TumblrDlError hierarchy
├── extractors.py      — media URL extraction per post type
├── models.py          — enums (DownloadStatus, MediaType) + dataclasses
└── utils.py           — sanitize_filename
```

## License

MIT
