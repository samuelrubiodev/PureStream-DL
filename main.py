"""
Backend FastAPI — Descargador multimedia self-hosted (Twitter/X e Instagram).

Restricción crítica: CERO almacenamiento en servidor. El backend actúa solo
como puente en RAM:
  1. /api/extract  -> ejecuta gallery-dl/yt-dlp con --dump-json (solo metadatos).
  2. /api/proxy    -> retransmite bytes del CDN de origen al navegador por stream.

Nunca se escribe ningún archivo multimedia en el disco del servidor.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import mimetypes
import os
import re
import socket
import sys
from typing import Any, AsyncIterator
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Sello de versión: sirve para confirmar que el contenedor corre la imagen
# nueva (ver /api/health). Si la versión no cuadra, el rebuild no se aplicó.
VERSION = "1.4.0"

# User-Agent opcional para gallery-dl. Instagram a veces rechaza cookies si el
# UA no coincide con el navegador origen de la sesión. Copia el UA de tu
# navegador (https://whatismybrowser.com/detect/what-is-my-user-agent/) y
# pásalo vía esta variable de entorno.
GALLERY_DL_UA = os.getenv("GALLERY_DL_UA", "")

# Tiempo máximo de ejecución de gallery-dl / yt-dlp (segundos).
EXTRACT_TIMEOUT = int(os.getenv("EXTRACT_TIMEOUT", "60"))
# Tiempo máximo de inactividad del proxy de streaming (segundos).
PROXY_TIMEOUT = float(os.getenv("PROXY_TIMEOUT", "300"))
# Tamaño del bloque al retransmitir bytes (bytes).
CHUNK_SIZE = 64 * 1024

# Fichero de cookies opcional (formato Netscape) para contenido restringido de
# Instagram/Twitter que requiere sesión. Definir con la variable de entorno
# COOKIES_FILE=/data/cookies.txt  (no se incluye en la imagen por defecto).
COOKIES_FILE = os.getenv("COOKIES_FILE", "")

# Refresh automático de sesión de Instagram vía Playwright (Opción A+D).
# Opcional: si Playwright no está instalado, la app sigue funcionando con
# cookies manuales (comportamiento previo). Ver ig_auth.py e
# INSTAGRAM_SESSION_REFRESH.md.
INSTAGRAM_AUTO_REFRESH = os.getenv("INSTAGRAM_AUTO_REFRESH", "1") == "1"
try:
    import ig_auth
    IG_AUTH_AVAILABLE = ig_auth.playwright_available()
except Exception as _ig_exc:  # noqa: BLE001
    ig_auth = None  # type: ignore[assignment]
    IG_AUTH_AVAILABLE = False
    print(f"[ig_auth] módulo no disponible: {_ig_exc}", file=sys.stderr)

# Lista blanca de dominios del CDN permitidos para el proxy. Evita SSRF:
# el backend solo retransmite desde los servidores de origen legítimos.
ALLOWED_HOST_SUFFIXES = (
    "twimg.com",        # CDN de imágenes/vídeo de Twitter  (pbs.twimg.com, video.twimg.com)
    "twitter.com",
    "x.com",
    "instagram.com",
    "cdninstagram.com", # scontent.cdninstagram.com, etc.
    "fbcdn.net",        # scontent-*.fbcdn.net (Instagram/Facebook)
    "akamaihd.net",     # respaldo de CDN de Twitter
)

VIDEO_EXTENSIONS = {"mp4", "webm", "m4v", "mov", "gif", "mkv"}

app = FastAPI(title="Media Downloader", version=VERSION)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def _startup_log() -> None:
    # Línea de log que delata la versión corriendo (para confirmar el rebuild).
    ready = _cookies_ready()
    domains = _cookies_domains() if ready else []
    ig_names = _instagram_cookie_names()
    ig_pw = IG_AUTH_AVAILABLE
    ig_st = bool(ig_auth and ig_auth.state_exists())
    print(f"[extract] Media Downloader v{VERSION} arrancando "
          f"(cookies={'sí' if ready else 'no'}, "
          f"domains={domains}, "
          f"instagram_cookie_names={ig_names}, "
          f"UA_gallery-dl={'sí' if GALLERY_DL_UA else 'no'}, "
          f"playwright={'sí' if ig_pw else 'no'}, "
          f"ig_state={'sí' if ig_st else 'no'})", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #

def host_allowed(host: str) -> bool:
    """
    Anti-SSRF. Permite:
      - Los CDN conocidos de Twitter/Instagram (lista blanca, sin resolución).
      - Cualquier OTRO host publico (para que la app sirva cientos de sitios
        soportados por gallery-dl/yt-dlp), PERO resolviendo su IP y rechazando
        si apunta a una red privada/loopback/link-local/reservada. Así no se
        puede abusar del proxy para reached servicios internos del homelab.
    """
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    if any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES):
        return True
    # Host arbitrario: resolver y exigir IP publica.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for _fam, _ty, _proto, _cn, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _cookies_domains() -> list[str]:
    """Dominios presentes en el cookies.txt (un fichero sirve para varios sitios)."""
    if not _cookies_ready():
        return []
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    domains: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            domains.add(parts[0].lstrip("."))
    return sorted(d for d in domains if d)


def _instagram_cookie_names() -> list[str]:
    """Devuelve los nombres de cookies de instagram.com (sin valores)."""
    if not _cookies_ready():
        return []
    names: list[str] = []
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.strip().split("\t")
                if len(parts) >= 7 and "instagram.com" in parts[0]:
                    names.append(parts[5])
    except OSError:
        pass
    return names


def safe_filename(name: str) -> str:
    """Sanea un nombre de archivo para usarlo en Content-Disposition."""
    name = (name or "media").strip()
    # Elimina caracteres problematicos para sistemas de archivos y cabeceras.
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(". ")
    return name[:120] or "media"


def is_video(ext: str | None, item_type: str | None) -> bool:
    if item_type == "video":
        return True
    return (ext or "").lower() in VIDEO_EXTENSIONS


# --------------------------------------------------------------------------- #
# Parseo de la salida --dump-json de gallery-dl / yt-dlp
# --------------------------------------------------------------------------- #

def _parse_gallery_dl(record: dict[str, Any]) -> dict[str, Any] | None:
    """Normaliza un registro de gallery-dl al formato del Frontend."""
    if not isinstance(record, dict):
        return None
    url = record.get("url")
    if not isinstance(url, str) or not url:
        return None
    # Rechazar pseudo-URLs no descargables directamente. gallery-dl emite
    # "ytdl:<https-url>" cuando delega un vídeo a yt-dlp (no hay URL directa del
    # CDN). Al rechazarlo, el fallback yt-dlp resuelve el vídeo con su URL
    # directa del CDN (proxyeable). Sin esto, el proxy daría 400 (esquema ytdl).
    if not url.startswith(("http://", "https://")):
        return None
    ext = (record.get("extension") or "").lower()
    if not ext:
        # Inferir extensión desde la URL; si no hay punto, es una URL de página
        # (p. ej. la del post), no un medio: la descartamos.
        path = urlparse(url).path
        if "." not in path.rsplit("/", 1)[-1]:
            return None
        ext = path.rsplit(".", 1)[-1].lower()
    fname = record.get("filename") or f"media_{record.get('id', '')}"
    # Instagram: la miniatura buena es display_url (jpg); video_url indica vídeo.
    is_vid = bool(record.get("video_url")) or is_video(ext, record.get("type"))
    return {
        "id": str(record.get("media_id") or record.get("shortcode")
                  or record.get("id") or fname),
        "type": "video" if is_vid else "image",
        "url": url,
        "thumbnail": record.get("thumbnail") or record.get("display_url") or url,
        "width": record.get("width"),
        "height": record.get("height"),
        "filename": f"{safe_filename(fname)}.{ext}" if ext else safe_filename(fname),
        "extension": ext,
        "duration": record.get("duration"),
    }


def _parse_yt_dlp(record: dict[str, Any]) -> dict[str, Any] | None:
    """Normaliza un registro JSON de yt-dlp. Elige el mejor formato directo."""
    if not isinstance(record, dict):
        return None
    formats = record.get("formats") or []
    # Mejor formato con URL directa: mayor bitrate total (tbr).
    best = None
    for f in formats:
        if not isinstance(f, dict) or not f.get("url"):
            continue
        if best is None or (f.get("tbr") or 0) > (best.get("tbr") or 0):
            best = f
    url = (best.get("url") if best else None) or record.get("url")
    if not url:
        return None
    src = best or record
    ext = (record.get("ext") or src.get("ext") or "mp4").lower()
    fid = str(record.get("id") or "media")
    return {
        "id": fid,
        "type": "video",
        "url": url,
        "thumbnail": record.get("thumbnail"),
        "width": src.get("width") or record.get("width"),
        "height": src.get("height") or record.get("height"),
        "filename": f"{safe_filename(fid)}.{ext}",
        "extension": ext,
        "duration": record.get("duration"),
    }


# --------------------------------------------------------------------------- #
# Ejecución de las herramientas externas
# --------------------------------------------------------------------------- #

# Límite de salida de las herramientas externas (bytes). Evita OOM si
# gallery-dl vuelca un perfil completo en lugar de un post.
MAX_TOOL_OUTPUT = 16 * 1024 * 1024


async def _run_dump_json(cmd: list[str]) -> tuple[str, str, int]:
    """
    Ejecuta un comando --dump-json. Devuelve (stdout_text, stderr_text, returncode).

    No interpreta el stdout: gallery-dl y yt-dlp usan formatos distintos, así
    que el parseo concreto lo hace quien llama (ver _gallery_dl_dicts / _yt_dlp_dicts).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=EXTRACT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("La extracción tardó demasiado (timeout).")

    if len(stdout) > MAX_TOOL_OUTPUT or len(stderr) > MAX_TOOL_OUTPUT:
        raise RuntimeError(
            f"Salida de {cmd[0]} demasiado grande ({len(stdout)} bytes stdout, "
            f"{len(stderr)} stderr). Probablemente se extrajo más de un post.")

    return (stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace").strip(),
            proc.returncode or 0)


def _gallery_dl_dicts(raw: str) -> tuple[list[dict[str, Any]], list[str]]:
    """
    gallery-dl --dump-json emite una lista de pares (jobs). Formatos reales:
      - Post:    [2, {post meta}]                          -> sin url, se ignora
      - Imagen:  [3, "https://...webp", {meta: width, extension, ...}]
                 la URL de descarga va como STRING suelto en pair[1]
      - Error:   [-1, {"error": ..., "message": ...}]
    Devuelve (medios, errores). Cada medio es el metadata dict con la clave
    "url" añadida desde pair[1].
    """
    raw = raw.strip()
    if not raw:
        return [], []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [], []
    out: list[dict[str, Any]] = []
    errs: list[str] = []

    def handle_dict(d: dict[str, Any]) -> None:
        if "error" in d:
            msg = d.get("message") or d.get("error")
            if msg:
                errs.append(str(msg))
        elif "url" in d:
            out.append(d)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            handle_dict(node)
            return
        if isinstance(node, list):
            # Par de medio: [codigo, "<url string>", {metadata}]
            if (len(node) == 3 and isinstance(node[0], int)
                    and isinstance(node[1], str) and isinstance(node[2], dict)):
                meta = dict(node[2])
                meta["url"] = node[1]  # URL de descarga (string suelto)
                out.append(meta)
                return
            # Par [codigo, {dict}] (post, error, o medio con url)
            if len(node) == 2 and isinstance(node[1], dict):
                handle_dict(node[1])
                return
            for x in node:
                walk(x)

    walk(data)
    return out, errs


def _yt_dlp_dicts(raw: str) -> list[dict[str, Any]]:
    """yt-dlp --dump-json emite JSONL compacto: un dict por línea."""
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _ingest(dicts: list, parse, items: list, tool: str) -> None:
    """Parsa dicts a items. Loguea en stderr si un dict falla al parsear."""
    for d in dicts:
        try:
            item = parse(d)
        except Exception as e:
            print(f"[extract] {tool}: fallo parse: {e} :: {repr(d)[:200]}",
                  file=sys.stderr)
            item = None
        if item:
            items.append(item)


def _log_shape(node: Any, depth: int = 0, maxd: int = 14) -> None:
    """Vuelca la forma (claves) del árbol JSON de gallery-dl al stderr.
    Recorre dict-values y los primeros elementos de cada lista para revelar
    dónde están realmente las URLs de los medios."""
    pad = "  " * depth
    if isinstance(node, dict):
        ks = list(node.keys())
        print(f"{pad}dict({len(ks)}) keys={ks[:40]}", file=sys.stderr)
        if depth < maxd:
            for k in ks:
                v = node[k]
                if isinstance(v, (dict, list)):
                    print(f"{pad}· {k}:", file=sys.stderr)
                    _log_shape(v, depth + 2, maxd)
    elif isinstance(node, list):
        print(f"{pad}list[{len(node)}]", file=sys.stderr)
        if depth < maxd:
            for x in node[:5]:
                _log_shape(x, depth + 1, maxd)


def _cookies_ready() -> bool:
    """True si hay un fichero de cookies configurado y presente en disco."""
    return bool(COOKIES_FILE) and os.path.isfile(COOKIES_FILE)


# yt-dlp (y a veces gallery-dl) intenta re-escribir el cookies.txt al salir
# para persistir cookies nuevas. Si el usuario monta el fichero read-only vía
# compose, eso falla y las herramientas no funcionan bien. Copiamos el fichero
# a una ruta writable del contenedor si es necesario.
_COOKIES_ACTIVE = ""


def _clean_cookies(src: str, dst: str) -> None:
    """
    Copia el cookies.txt descartando duplicados: quedarse con la entrada de
    mayor expiración (timestamp) para cada (domain, path, name). Las cookies
    viejas acumuladas al exportar varias veces pueden confundir a gallery-dl.
    """
    latest: dict[tuple[str, str, str], tuple[int, str]] = {}
    header: list[str] = []
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                if line.startswith("#"):
                    header.append(line)
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            try:
                expires = int(parts[4])
            except ValueError:
                expires = 0
            key = (parts[0], parts[2], parts[5])  # domain, path, name
            prev = latest.get(key)
            if prev is None or expires > prev[0]:
                latest[key] = (expires, line)
    with open(dst, "w", encoding="utf-8") as f:
        for h in header:
            f.write(h + "\n")
        # latest[key] = (expires, line_string); ordenar por expiración.
        for _, line in sorted(latest.values(), key=lambda v: v[0]):
            f.write(line + "\n")


def _cookies_path() -> str:
    """
    Devuelve una ruta de cookies usable para las herramientas.

    NUNCA usamos COOKIES_FILE directamente: yt-dlp reescribe el fichero al
    cerrar y puede corromperlo o truncarlo. Siempre trabajamos desde una copia
    en /tmp, refrescada desde el original (subida web o montaje read-only)
    cuando el original es más reciente o distinto de tamaño. Además se limpian
    duplicados para evitar cookies viejas mezcladas con nuevas.
    """
    global _COOKIES_ACTIVE
    import shutil
    if not _cookies_ready():
        return ""
    tmp = "/tmp/cookies_active.txt"
    need_copy = True
    if os.path.isfile(tmp) and os.path.isfile(COOKIES_FILE):
        try:
            orig_size = os.path.getsize(COOKIES_FILE)
            tmp_size = os.path.getsize(tmp)
            orig_mtime = os.path.getmtime(COOKIES_FILE)
            tmp_mtime = os.path.getmtime(tmp)
            # Refrescar si el original cambió (más reciente o distinto tamaño).
            if orig_size == tmp_size and abs(tmp_mtime - orig_mtime) < 1:
                need_copy = False
        except OSError:
            need_copy = True
    if need_copy:
        _clean_cookies(COOKIES_FILE, tmp)
        shutil.copy2(tmp, tmp + ".clean")  # copia de respaldo para debug
        os.chmod(tmp, 0o600)
        print(f"[cookies] refreshed+cleaned {COOKIES_FILE} -> {tmp} "
              f"({os.path.getsize(COOKIES_FILE)} bytes -> {os.path.getsize(tmp)} bytes, "
              f"{len(_instagram_cookie_names())} ig names)",
              file=sys.stderr)
    _COOKIES_ACTIVE = tmp
    return tmp


def _build_cmd(tool: str, url: str, variant: str = "default", skip_cookies: bool = False) -> list[str]:
    """
    Construye la línea de comandos --dump-json según la herramienta.

    variant="graphql" (gallery-dl) fuerza el extractor legacy de Instagram,
    a veces funciona cuando el API nuevo bloquea la IP del servidor.
    skip_cookies=True prueba sin cookies (último recurso para posts públicos).
    """
    cookies_path = "" if skip_cookies else _cookies_path()
    if tool == "gallery-dl":
        cmd = ["gallery-dl", "--dump-json"]
        if cookies_path:
            cmd += ["--cookies", cookies_path]
        if GALLERY_DL_UA:
            cmd += ["-o", f"user-agent={GALLERY_DL_UA}"]
        if variant == "graphql":
            cmd += ["-o", "api=graphql"]
        cmd.append(url)
        return cmd
    # yt-dlp
    cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--no-playlist"]
    if cookies_path:
        cmd += ["--cookies", cookies_path]
    cmd.append(url)
    return cmd


async def extract_media(url: str) -> dict[str, Any]:
    """
    Extrae metadatos de la publicacion sin descargar contenido.

    Estrategia: intenta primero gallery-dl (ideal para imágenes/carruseles y
    vídeos de Twitter/Instagram). Si NO obtiene ningún registro, reintenta con
    yt-dlp (mejor para vídeos largos y Reels). yt-dlp no extrae imágenes, por
    eso gallery-dl es la vía principal para carruseles de fotos.
    """
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    # Hoist para poder incluir muestras brutas en el diagnóstico al usuario.
    gd_raw = gd_err = yd_raw = yd_err = ""
    gd_dicts: list = []
    gd_errs: list = []

    # 1) gallery-dl — vía principal (imágenes, carruseles, GIFs y vídeos).
    try:
        gd_raw, gd_err, _ = await _run_dump_json(_build_cmd("gallery-dl", url))
        gd_dicts, gd_errs = _gallery_dl_dicts(gd_raw)
        # Log de resumen SIEMPRE: dicts, errs, bytes de stdout/stderr.
        err_preview = " | ".join(gd_errs)[:200] if gd_errs else ""
        print(f"[extract] gallery-dl: dicts={len(gd_dicts)} errs={len(gd_errs)} "
              f"stdout_bytes={len(gd_raw)} stderr_bytes={len(gd_err)} "
              f"err_preview={err_preview!r}", file=sys.stderr)
        # Si no sacamos medios pero gallery-dl devolvió algo, vuelca la forma
        # del árbol (claves) para ver dónde están las URLs reales.
        if not gd_dicts and gd_raw.strip():
            try:
                print("[extract] gallery-dl SHAPE:", file=sys.stderr)
                _log_shape(json.loads(gd_raw))
            except Exception as e:
                print(f"[extract] no se pudo volcar la forma: {e}", file=sys.stderr)
        _ingest(gd_dicts, _parse_gallery_dl, items, "gallery-dl")
        # Si obtuvimos items, ignoramos warnings; si no, exponemos el error real
        # de gallery-dl (incluido el mensaje del dict de error, p. ej. login).
        if not items:
            parts = []
            if gd_errs:
                parts.append(" | ".join(gd_errs))
            if gd_err:
                parts.append(gd_err)
            if parts:
                errors.append("[gallery-dl] " + " | ".join(parts))

        # 1b) Reintento gallery-dl con api=graphql (extractor legacy de
        # Instagram). Cuando Instagram bloquea/banea la IP del servidor con el
        # API nuevo, el legacy a veces responde (solo ~5 posts y a menor
        # calidad, según el autor de gallery-dl).
        if not items and "instagram" in url.lower():
            try:
                print("[extract] gallery-dl reintento api=graphql", file=sys.stderr)
                gql_raw, gql_err, _ = await _run_dump_json(
                    _build_cmd("gallery-dl", url, variant="graphql", skip_cookies=False))
                gql_dicts, gql_errs = _gallery_dl_dicts(gql_raw)
                print(f"[extract] gallery-dl graphql: dicts={len(gql_dicts)} "
                      f"errs={len(gql_errs)} stdout_bytes={len(gql_raw)} "
                      f"stderr_bytes={len(gql_err)}", file=sys.stderr)
                _ingest(gql_dicts, _parse_gallery_dl, items, "gallery-dl-graphql")
                if gql_errs and not items:
                    errors.append(f"[gallery-dl graphql] {' | '.join(gql_errs)}")
            except (RuntimeError, FileNotFoundError) as e:
                errors.append(f"[gallery-dl graphql] {e}")

        # 1c) Último recurso para Instagram públicos: intentar SIN cookies.
        # Si las cookies están siendo rechazadas por Meta (redirect to login),
        # a veces el endpoint público responde un par de veces sin sesión.
        if not items and "instagram" in url.lower():
            try:
                print("[extract] gallery-dl reintento sin cookies (post público)",
                      file=sys.stderr)
                pub_raw, pub_err, _ = await _run_dump_json(
                    _build_cmd("gallery-dl", url, skip_cookies=True))
                pub_dicts, pub_errs = _gallery_dl_dicts(pub_raw)
                print(f"[extract] gallery-dl no-cookies: dicts={len(pub_dicts)} "
                      f"errs={len(pub_errs)} stdout_bytes={len(pub_raw)} "
                      f"stderr_bytes={len(pub_err)}", file=sys.stderr)
                _ingest(pub_dicts, _parse_gallery_dl, items, "gallery-dl-public")
                if pub_errs and not items:
                    errors.append(f"[gallery-dl public] {' | '.join(pub_errs)}")
            except (RuntimeError, FileNotFoundError) as e:
                errors.append(f"[gallery-dl public] {e}")
    except (RuntimeError, FileNotFoundError) as e:
        errors.append(f"[gallery-dl] {e}")

    # 2) Fallback yt-dlp SOLO si gallery-dl no devolvió nada (vídeos largos/Reels).
    if not items:
        try:
            yd_raw, yd_err, _ = await _run_dump_json(_build_cmd("yt-dlp", url))
            yd_dicts = _yt_dlp_dicts(yd_raw)
            print(f"[extract] yt-dlp: dicts={len(yd_dicts)} stdout_bytes={len(yd_raw)} "
                  f"stderr_bytes={len(yd_err)}", file=sys.stderr)
            _ingest(yd_dicts, _parse_yt_dlp, items, "yt-dlp")
            if yd_err and not items:
                errors.append(f"[yt-dlp] {yd_err}")
        except (RuntimeError, FileNotFoundError) as e:
            errors.append(f"[yt-dlp] {e}")

    # 3) Auto-refresh de sesión de Instagram (Opción D): si todo falló con la
    #    marca de "redirect to login", abrir Chromium vía Playwright para
    #    refrescar la sesión y reintentar gallery-dl/yt-dlp una vez. Silencioso
    #    en el caso común (reutiliza storage_state, sin pedir nada al usuario).
    if (not items and "instagram" in url.lower() and INSTAGRAM_AUTO_REFRESH
            and ig_auth is not None and IG_AUTH_AVAILABLE
            and COOKIES_FILE and ig_auth.refresh_off_cooldown()):
        detail_low = " | ".join(errors).lower()
        # Disparador: marca de login-redirect en los errores, o no hay cookies
        # pero sí storage_state que pueda refrescarse.
        if (any(m in detail_low for m in ig_auth.LOGIN_REDIRECT_MARKERS)
                or (not _cookies_ready() and ig_auth.state_exists())):
            try:
                print("[extract] instagram: sesión rechazada -> refrescando vía Playwright",
                      file=sys.stderr)
                res = await ig_auth.refresh_session_silent(COOKIES_FILE)
                print(f"[extract] refresh: {res}", file=sys.stderr)
                if res.get("status") == "ok":
                    # Reintentar gallery-dl (y yt-dlp) con las cookies recién
                    # escritas. _cookies_path() detecta el cambio y re-copia.
                    gd_raw2, gd_err2, _ = await _run_dump_json(_build_cmd("gallery-dl", url))
                    gd_dicts2, gd_errs2 = _gallery_dl_dicts(gd_raw2)
                    _ingest(gd_dicts2, _parse_gallery_dl, items, "gallery-dl-after-refresh")
                    if not items:
                        yd_raw2, yd_err2, _ = await _run_dump_json(_build_cmd("yt-dlp", url))
                        _ingest(_yt_dlp_dicts(yd_raw2), _parse_yt_dlp, items,
                                "yt-dlp-after-refresh")
                    if not items and gd_errs2:
                        errors.append("[ig-refresh] " + " | ".join(gd_errs2))
                elif res.get("status") in ("needs_2fa", "needs_login"):
                    errors.append("[ig-refresh] " + res.get("error", ""))
                else:
                    errors.append("[ig-refresh] " + res.get("error", "refresco falló."))
            except Exception as e:
                import traceback
                errors.append(f"[ig-refresh] excepción: {e}")
                print(f"[extract] refresh excepción: {e}\n{traceback.format_exc()}",
                      file=sys.stderr)

    if not items:
        # Mensaje al usuario: limpio, sin detalles internos de subprocess.
        detail = " | ".join(errors) or "No se encontro multimedia o la URL es privada/invalida."
        # Pistas de autenticación / User-Agent mismatch / IP ban / tool outdated.
        dlow = detail.lower()
        if any(k in dlow for k in ("login", "cookie", "401", "unauthorized", "private")):
            detail += " → verifica COOKIES_FILE (cookies Netscape frescas)"
        if "redirect to home" in dlow or "redirect to login" in dlow:
            detail += ("; Instagram rechaza la sesión. Pulsa 🔑 'Renovar sesión' en la "
                       "web para refrescarla automáticamente con Playwright. Si aun así "
                       "falla: (1) GALLERY_DL_UA no coincide con el navegador que exportó "
                       "las cookies, (2) cookies incompletas (sessionid, ds_user_id, "
                       "csrftoken), o (3) la sesión murió del todo y hace falta re-login "
                       "con credenciales (INSTAGRAM_USERNAME/PASSWORD o login por la web).")
        # Diagnóstico completo SOLO en logs del servidor (no va al cliente).
        print(f"[extract] FAILED extract for {url}: {detail}", file=sys.stderr)
        if gd_raw.strip():
            print(f"[extract] gallery-dl stdout[:2500]={gd_raw[:2500]!r}", file=sys.stderr)
        if gd_err:
            print(f"[extract] gallery-dl stderr[:500]={gd_err[:500]!r}", file=sys.stderr)
        if yd_raw.strip():
            print(f"[extract] yt-dlp stdout[:500]={yd_raw[:500]!r}", file=sys.stderr)
        raise RuntimeError(detail)

    return {"items": items, "errors": errors}


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
async def index():
    """Sirve el Frontend (SPA)."""
    with open(os.path.join(TEMPLATES_DIR, "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.post("/api/extract")
async def api_extract(payload: dict[str, Any]):
    """Paso 1-3: recibe una URL y devuelve el array de medios parseado."""
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Falta el parámetro 'url'.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="URL no válida.")

    try:
        result = await extract_media(url)
    except RuntimeError as e:
        # Error limpio: la app no se congela, el Frontend lo muestra.
        raise HTTPException(status_code=422, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:  # seguridad: captura final ante fallos imprevistos
        import traceback
        print(f"[extract] UNEXPECTED ERROR for {url}: {e}\n{traceback.format_exc()}",
              file=sys.stderr)
        raise HTTPException(status_code=500, detail="Error interno del servidor")

    return result


@app.get("/api/cookies")
async def cookies_status():
    """
    Estado de cookies: si están configuradas, si el fichero es escribible (o está
    montado read-only por compose) y qué dominios contiene (un único cookies.txt
    Netscape puede servir a varios sitios a la vez).
    """
    configured = _cookies_ready()
    writable = bool(COOKIES_FILE) and os.access(os.path.dirname(os.path.abspath(COOKIES_FILE)) or ".",
                                                os.W_OK)
    return {
        "configured": configured,
        "path": COOKIES_FILE or "",
        "read_only": configured and not os.access(COOKIES_FILE, os.W_OK),
        "writable_dir": writable,
        "domains": _cookies_domains(),
    }


@app.post("/api/cookies")
async def cookies_upload(file: UploadFile = File(...)):
    """
    Sube un cookies.txt (formato Netscape) desde el navegador.

    Autenticación/CONFIG (no multimedia): se guarda en disco del servidor porque
    gallery-dl/yt-dlp lo requieren. Un ÚNICO fichero con varios dominios
    (instagram.com, x.com, twitter.com, ...) sirve para todos esos sitios a la
    vez. Peligro: concede acceso total a las cuentas -> no lo compartas.

    Alternativa sin web: monta tu cookies.txt vía docker-compose (read-only); en
    ese caso este endpoint devolverá error indicando que edites el fichero host.
    """
    if not COOKIES_FILE:
        raise HTTPException(
            status_code=400,
            detail="COOKIES_FILE no configurado. Define la variable de entorno "
                   "(p. ej. /data/cookies.txt) y monta el volumen en el contenedor.",
        )
    if os.path.isfile(COOKIES_FILE) and not os.access(COOKIES_FILE, os.W_OK):
        raise HTTPException(
            status_code=403,
            detail="El cookies.txt está montado read-only (vía compose). "
                   "Edítalo en el host y reinicia, o quita el ':ro' del montaje.",
        )
    raw = await file.read()
    # Límite de tamaño: un cookies.txt normal no supera 5 MB.
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El fichero de cookies es demasiado grande (>5 MB).")
    text = raw.decode("utf-8", "replace")
    # Validación de formato Netscape: dominio, flag, path, secure, expiración,
    # nombre, valor. Al menos una línea válida.
    domains: set[str] = set()
    valid_lines = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, flag, path, secure, expires, name, value = parts[:7]
        if not domain or not path.startswith("/") or secure not in ("0", "1"):
            continue
        try:
            int(expires)
        except ValueError:
            continue
        if not name:
            continue
        domains.add(domain.lstrip("."))
        valid_lines += 1
    if valid_lines == 0:
        raise HTTPException(status_code=400, detail="No parece un cookies.txt Netscape válido.")
    os.makedirs(os.path.dirname(os.path.abspath(COOKIES_FILE)), exist_ok=True)
    tmp = COOKIES_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, COOKIES_FILE)  # escritura atómica
        os.chmod(COOKIES_FILE, 0o600)   # solo el propietario puede leer las credenciales
        print(f"[cookies] guardado {COOKIES_FILE}: {len(raw)} bytes, domains={sorted(domains)}",
              file=sys.stderr)
    except OSError as e:
        raise HTTPException(
            status_code=403,
            detail=f"No se pudo escribir el cookies.txt (¿montado read-only?): {e}",
        )
    return {"configured": True, "bytes": len(raw), "domains": sorted(domains)}


# --------------------------------------------------------------------------- #
# Refresh / login de sesión de Instagram (Playwright — Opción A+D)
# --------------------------------------------------------------------------- #

@app.get("/api/auth/instagram/state")
async def ig_auth_state():
    """
    Estado del sistema de refresh de Instagram. El frontend lo usa para decidir
    qué UI mostrar (solo código 2FA vs login completo con usuario/contraseña):
      - playwright: si Playwright está instalado en el contenedor.
      - auto_refresh: si el auto-refresh silencioso está activo.
      - state_file: si hay storage_state persistido (sesión reutilizable).
      - env_creds: si hay INSTAGRAM_USERNAME/PASSWORD (login silencioso posible).
      - cookies: si hay cookies.txt presente.
    """
    return {
        "playwright": IG_AUTH_AVAILABLE,
        "auto_refresh": INSTAGRAM_AUTO_REFRESH,
        "state_file": bool(ig_auth and ig_auth.state_exists()),
        "env_creds": bool(ig_auth and ig_auth.env_creds_set()),
        "cookies": _cookies_ready(),
    }


@app.post("/api/auth/instagram/refresh")
async def ig_auth_refresh():
    """
    Refresco silencioso MANUAL (sin interacción): reutiliza storage_state, abre
    IG en Chromium headless y re-exporta cookies a COOKIES_FILE. Úsalo cuando
    /api/extract avise de sesión caducada y quieras forzar el refresco ya.
    Devuelve {"status":"ok","cookies":N} o {status:needs_login|needs_2fa|error}.
    """
    if ig_auth is None or not IG_AUTH_AVAILABLE:
        raise HTTPException(status_code=503,
                            detail="Playwright no instalado en este contenedor "
                                   "(rebuild con el Dockerfile actualizado).")
    if not COOKIES_FILE:
        raise HTTPException(status_code=400, detail="COOKIES_FILE no configurado.")
    res = await ig_auth.refresh_session_silent(COOKIES_FILE)
    # needs_login/needs_2fa/error se devuelven 200 con status para que el
    # frontend actúe (mostrar login / pedir 2FA / mostrar error).
    return JSONResponse(res)


@app.post("/api/auth/instagram")
async def ig_auth_login(payload: dict[str, Any]):
    """
    Inicia login interactivo de Instagram. Creds: body (username/password)
    opcional > env INSTAGRAM_USERNAME/PASSWORD. Si IG pide 2FA, el frontend
    consulta /api/auth/instagram/status y envía el código a /2fa.
    La password del body NO se persiste (solo se usa para esta sesión).
    """
    if ig_auth is None or not IG_AUTH_AVAILABLE:
        raise HTTPException(status_code=503, detail="Playwright no instalado.")
    if not COOKIES_FILE:
        raise HTTPException(status_code=400, detail="COOKIES_FILE no configurado.")
    username = (payload.get("username") or "").strip() or None
    password = (payload.get("password") or "").strip() or None
    res = await ig_auth.start_login(COOKIES_FILE, username=username, password=password)
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("error", "No se pudo iniciar login."))
    return res


@app.get("/api/auth/instagram/status")
async def ig_auth_status(session_id: str = Query(..., description="ID de sesión de login")):
    """Polling del estado de un login en curso (logging_in/needs_2fa/ok/error)."""
    if ig_auth is None:
        raise HTTPException(status_code=503, detail="Playwright no instalado.")
    return ig_auth.session_status(session_id)


@app.post("/api/auth/instagram/2fa")
async def ig_auth_2fa(payload: dict[str, Any]):
    """Entrega el código 2FA del usuario a la sesión de login en espera."""
    if ig_auth is None:
        raise HTTPException(status_code=503, detail="Playwright no instalado.")
    session_id = (payload.get("session_id") or "").strip()
    code = (payload.get("code") or "").strip()
    if not session_id or not code:
        raise HTTPException(status_code=400, detail="Falta session_id o code.")
    res = await ig_auth.submit_2fa(session_id, code)
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("error", "2FA falló."))
    return res


@app.get("/api/proxy")
async def api_proxy(
    request: Request,
    url: str = Query(..., description="URL directa del CDN de origen"),
    filename: str = Query("media", description="Nombre sugerido para la descarga"),
    inline: bool = Query(False, description="Modo inline (preview) en vez de descarga attachment"),
):
    """
    Proxy de streaming. Descarga por stream=True del CDN de origen y
    retransmite los bytes al navegador. Nada toca el disco del servidor: los
    chunk viajan por RAM hacia el StreamingResponse.

    inline=True -> Content-Disposition: inline (para renderizar previews en el
    grid). Por defecto -> attachment (fuerza descarga). El proxy envía un
    User-Agent realista y SIN Referer, evitando el hotlink-403 de los CDN de
    Twitter/Instagram cuando el navegador pide el medio directamente.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Esquema no permitido.")
    if not host_allowed(parsed.netloc):
        # Anti-SSRF: solo se permite retransmitir desde los CDN de Twitter/Instagram.
        raise HTTPException(status_code=403, detail="Dominio de origen no permitido.")

    fname = safe_filename(filename)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36",
        "Accept": "*/*",
        # Sin Referer -> evita hotlink-403 del CDN.
    }
    # Passthrough de Range: permite que <video preload=metadata> pida solo un
    # prefijo del vídeo para mostrar un frame (preview) sin bajarlo entero.
    range_hdr = request.headers.get("range")
    if range_hdr:
        # Validar formato bytes=start-end (evita enviar basura al origen).
        if not re.fullmatch(r"bytes=\d+-\d*", range_hdr.strip(), re.IGNORECASE):
            raise HTTPException(status_code=400, detail="Cabecera Range no válida.")
        headers["Range"] = range_hdr

    # Anti-SSRF: desactivamos follow_redirects y seguimos manualmente, re-
    # validando cada Location con host_allowed(). Así un CDN permitido no puede
    # redirigir a 169.254.169.254 ni a localhost.
    client = httpx.AsyncClient(timeout=httpx.Timeout(PROXY_TIMEOUT, connect=15.0),
                                follow_redirects=False)

    async def follow(method: str, target_url: str, hops: int = 0) -> httpx.Response:
        if hops > 5:
            raise HTTPException(status_code=502, detail="Demasiados redirects del origen.")
        tparsed = urlparse(target_url)
        if tparsed.scheme not in ("http", "https"):
            raise HTTPException(status_code=400, detail="Esquema de redirect no permitido.")
        if not host_allowed(tparsed.netloc):
            raise HTTPException(status_code=403, detail="Dominio de redirect no permitido.")
        req = client.build_request(method, target_url, headers=headers)
        try:
            resp = await client.send(req, stream=True)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Error de red con el origen: {e}")
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            await resp.aclose()
            if not location:
                raise HTTPException(status_code=502, detail="Redirect sin Location.")
            # Location relativa: resolver contra URL actual.
            if location.startswith("//"):
                location = tparsed.scheme + ":" + location
            elif location.startswith("/"):
                location = f"{tparsed.scheme}://{tparsed.netloc}{location}"
            elif not re.match(r"https?://", location):
                location = f"{tparsed.scheme}://{tparsed.netloc}{location}"
            return await follow(method, location, hops + 1)
        return resp

    try:
        resp = await follow("GET", url)
    except Exception:
        await client.aclose()
        raise

    if resp.status_code >= 400:
        body = await resp.aread()
        await resp.aclose()
        await client.aclose()
        # Logueamos el cuerpo del error del origen en servidor, no al cliente.
        print(f"[proxy] origin error {resp.status_code} for {url}: "
              f"{body[:500].decode('utf-8', 'replace')}", file=sys.stderr)
        raise HTTPException(status_code=502, detail="El origen devolvió un error.")

    # Content-Type: preferimos el del origen; si no, inferimos de la URL.
    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
    if not ctype:
        ctype = (mimetypes.guess_type(urlparse(str(resp.url)).path)[0]
                 or mimetypes.guess_type(fname)[0]
                 or "application/octet-stream")

    # Límite de tamaño: evita descargas descomunalmente grandes o streams
    # infinitos. Se basa en Content-Length cuando existe; el generator también
    # corta si se supera el límite durante el stream.
    MAX_PROXY_BYTES = int(os.getenv("MAX_PROXY_BYTES", str(2 * 1024 * 1024 * 1024)))  # 2 GB
    try:
        cl = int(resp.headers.get("content-length", "0"))
    except ValueError:
        cl = 0
    if cl and cl > MAX_PROXY_BYTES:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="El medio excede el tamaño máximo permitido.")

    if inline:
        disposition = "inline"
        cache = "public, max-age=3600"
    else:
        disposition = "attachment; filename=\"{}\"; filename*=UTF-8''{}".format(
            fname.replace('"', ""), quote(fname)
        )
        cache = "no-store"

    async def gen() -> AsyncIterator[bytes]:
        nonlocal resp
        total = 0
        try:
            async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                total += len(chunk)
                if total > MAX_PROXY_BYTES:
                    # Origen está enviando más de lo anunciado o no hay
                    # Content-Length: cortamos el stream para no consumir RAM infinita.
                    print(f"[proxy] aborted stream for {url}: exceeded {MAX_PROXY_BYTES} bytes",
                          file=sys.stderr)
                    break
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    # Passthrough de cabeceras de rango para respuestas 206 (preview de vídeo).
    out_headers = {
        "Content-Disposition": disposition,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": cache,
        "Accept-Ranges": resp.headers.get("accept-ranges", "bytes"),
    }
    if resp.status_code == 206:
        cr = resp.headers.get("content-range")
        if cr:
            out_headers["Content-Range"] = cr
        cl = resp.headers.get("content-length")
        if cl:
            out_headers["Content-Length"] = cl

    return StreamingResponse(
        gen(),
        media_type=ctype,
        status_code=resp.status_code,
        headers=out_headers,
    )


@app.get("/api/health")
async def health():
    """Sonda de salud + versión (para confirmar que la imagen es la nueva)."""
    return {
        "status": "ok",
        "version": VERSION,
        "cookies": _cookies_ready(),
        "gallery_dl_ua": bool(GALLERY_DL_UA),
        "playwright": IG_AUTH_AVAILABLE,
        "ig_state": bool(ig_auth and ig_auth.state_exists()),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)