# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

PureStream-DL is a self-hosted FastAPI web app that lets users download media from social networks and hundreds of gallery-dl/yt-dlp supported sites. The critical architectural constraint is **zero server-side media storage**: the server extracts metadata and proxies byte streams from the origin CDN to the user's browser in RAM; the browser saves files locally.

## Run / build / test commands

Docker is the primary runtime. The container installs Python deps, `gallery-dl`, `yt-dlp`, `ffmpeg`, the Tailwind CSS standalone binary, and builds frontend assets during the image build.

```bash
# Build and run
docker compose up -d --build

# Check version (useful to confirm a rebuild actually loaded)
curl -s localhost:8000/api/health

# Run parser self-check (inside the container)
docker compose run --rm media-downloader python test_parse.py

# Force a clean rebuild (updates gallery-dl / yt-dlp to latest)
docker compose build --no-cache && docker compose up -d
```

Local development without Docker:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt gallery-dl yt-dlp
# Generate build artifacts that are normally produced in the Dockerfile
python3 static/gen_icons.py
./tailwindcss -i src/input.css -o static/tailwind.css --minify
python test_parse.py          # self-check, no network required
uvicorn main:app --reload
```

There is no separate lint / test runner beyond `test_parse.py` and `py_compile`. Run `python -m py_compile main.py` before committing.

## High-level architecture

```
Frontend (templates/index.html, static/)
  ├── PWA shell (manifest.json, sw.js, generated icons)
  ├── URL input → POST /api/extract
  ├── Grid with checkboxes → GET /api/proxy?url=...&filename=...
  └── Cookie upload → POST /api/cookies

Backend (main.py)
  ├── /api/extract
  │     └── extract_media(url) → gallery-dl --dump-json (primary)
  │         └── fallback to yt-dlp --dump-json if no media items
  ├── /api/proxy
  │     └── StreamingResponse proxy to origin CDN (http/https only)
  │         └── Anti-SSRF: known CDN hosts allowed; other hosts resolved
  │             and rejected if they point to private/loopback/link-local IPs.
  │             Redirects are followed manually and each hop is re-validated;
  │             never turn this into a general open proxy.
  ├── /api/cookies
  │     └── Single Netscape cookies.txt per COOKIES_FILE env var,
  │         usable by multiple domains (Instagram, X/Twitter, etc.)
  └── /api/health → version & cookie status
```

`extract_media` is the most fragile area: gallery-dl emits a nested list structure (pairs `[int, metadata_dict]` plus `[int, url_string, metadata_dict]` for media items), not JSONL. `_gallery_dl_dicts()` recursively walks that tree and merges the bare URL string into the metadata dict. yt-dlp emits standard JSONL. Any parser change must be validated against real tool output (see `_log_shape` for diagnostics).

## Important domain rules

- **Never commit `cookies.txt` or the `data/` directory.** They contain session credentials and are excluded by `.gitignore`.
- `static/tailwind.css`, `static/icon-192.png`, and `static/icon-512.png` are generated during the Docker build and also excluded from git.
- The proxy requires the target host to be either a known social CDN or a public IP. Do not relax this into a general open proxy.
- `cookies.txt` is never served or exposed to clients; uploaded cookies are validated as Netscape format, stored with `0600` permissions, and copied to a writable `/tmp` path for the CLI tools.
- Subprocess stderr/stdout from gallery-dl/yt-dlp is logged server-side only; error responses to the client are sanitized to avoid leaking credentials or internal details.
- `VERSION` in `main.py` is the single source of truth; `/api/health` reports it.

## Cookies and PWA notes (from README)

- One Netscape-format `cookies.txt` can contain cookies for multiple domains; the same file serves Instagram, X/Twitter, and other sites. Provide it either via the web upload button or by mounting a file through `docker-compose.yml`.
- Android Chrome only shows the PWA install prompt over **HTTPS** (or `localhost`). HTTP LAN access will not offer installation.
- If Instagram rejects valid cookies with a login redirect, set `GALLERY_DL_UA` to the User-Agent of the browser used to export the cookies file.