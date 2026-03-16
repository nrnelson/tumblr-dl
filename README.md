# tumblr-dl

A CLI tool for downloading media (images, videos, audio) from Tumblr blogs using the Tumblr API v2 with OAuth authentication.

## Features

- Downloads images, videos, and audio from any Tumblr blog
- Extracts embedded images from text and answer posts
- Extracts video URLs from embedded iframes
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
git clone https://github.com/yourusername/tumblr-dl.git
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
| Video | Direct video URLs and embedded iframe sources |
| Audio | Direct audio URLs |
| Text | Embedded `<img>` tags in post body |
| Answer | Embedded `<img>` tags in answer body |

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
├── cli.py             — argparse + main() entry point
├── client.py          — TumblrClient wrapper (config loading + API)
├── downloader.py      — download_item() with DedupStrategy ABC
├── exceptions.py      — TumblrDlError hierarchy
├── extractors.py      — media URL extraction per post type
├── models.py          — enums (DownloadStatus, MediaType) + dataclasses
└── utils.py           — sanitize_filename
```

## License

MIT
