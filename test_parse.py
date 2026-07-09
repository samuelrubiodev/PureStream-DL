"""
Self-check mínimo de los parseadores (sin red, sin tools).
  Local sin deps:  python test_parse.py  -> imprime "skip" y sale 0.
  En contenedor:   docker compose run --rm media-downloader python test_parse.py
"""
import sys

try:
    from main import _parse_gallery_dl, _parse_yt_dlp, host_allowed, safe_filename
except ImportError as e:
    print("skip (faltan dependencias, ejecútalo en el contenedor):", e)
    sys.exit(0)

# --- gallery-dl: tweet con imagen -------------------------------------------
g_img = _parse_gallery_dl({
    "url": "https://pbs.twimg.com/media/ABC.jpg", "width": 1200, "height": 800,
    "extension": "jpg", "filename": "GrXAbc", "id": "123",
})
assert g_img and g_img["type"] == "image" and g_img["width"] == 1200
assert g_img["filename"] == "GrXAbc.jpg"

# --- gallery-dl: vídeo (gif servido como mp4 en Twitter) ---------------------
g_vid = _parse_gallery_dl({
    "url": "https://video.twimg.com/ext_tw_video/1/pu/vid/1280x720/Abc.mp4",
    "extension": "mp4", "filename": "vid", "id": "9",
})
assert g_vid and g_vid["type"] == "video"

# --- yt-dlp: vídeo con múltiples formatos -----------------------------------
y_vid = _parse_yt_dlp({
    "id": "Reel1", "ext": "mp4", "thumbnail": "https://scontent.cdninstagram.com/t.jpg",
    "width": 1080, "height": 1920, "duration": 12.3,
    "formats": [
        {"url": "https://video.twimg.com/lo.mp4", "tbr": 300},
        {"url": "https://video.twimg.com/hi.mp4", "tbr": 2500, "width": 1080, "height": 1920},
    ],
})
assert y_vid and y_vid["type"] == "video"
assert y_vid["url"].endswith("hi.mp4"), "debe elegir el formato de mayor tbr"
assert y_vid["filename"] == "Reel1.mp4" and y_vid["duration"] == 12.3

# --- anti-SSRF --------------------------------------------------------------
assert host_allowed("pbs.twimg.com") and host_allowed("scontent-abc.cdninstagram.com")
assert host_allowed("video.twimg.com")
assert not host_allowed("127.0.0.1") and not host_allowed("10.0.0.1")
assert not host_allowed("twimg.com.evil.com"), "no debe coincidir por sufijo falso"

# --- saneo de nombres -------------------------------------------------------
assert safe_filename("a/b:c?d*.mp4") == "a_b_c_d_.mp4"
assert safe_filename("") == "media"

print("OK: parseadores y validaciones pasan.")