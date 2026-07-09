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
VERSION = "1.3.2"

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
    print(f"[extract] Media Downloader v{VERSION} arrancando "
          f"(cookies={'sí' if ready else 'no'}, "
          f"domains={domains}, "
          f"UA_gallery-dl={'sí' if GALLERY_DL_UA else 'no'})", file=sys.stderr)


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


def _build_cmd(tool: str, url: str) -> list[str]:
    """Construye la línea de comandos --dump-json según la herramienta."""
    # Solo pasamos --cookies si el fichero existe (si no, gallery-dl/yt-dlp
    # abortan con "file not found"). Instagram casi siempre requiere cookies.
    use_cookies = _cookies_ready()
    if tool == "gallery-dl":
        cmd = ["gallery-dl", "--dump-json"]
        if use_cookies:
            cmd += ["--cookies", COOKIES_FILE]
        if GALLERY_DL_UA:
            cmd += ["-o", f"user-agent={GALLERY_DL_UA}"]
        cmd.append(url)
        return cmd
    # yt-dlp
    cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--no-playlist"]
    if use_cookies:
        cmd += ["--cookies", COOKIES_FILE]
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

    if not items:
        # Sin resultados: exponemos los errores de las herramientas.
        detail = " | ".join(errors) or "No se encontro multimedia o la URL es privada/invalida."
        # Muestras brutas para diagnóstico (se ven en el error de la web): revelan
        # la estructura real que devolvió gallery-dl si el parser no la reconoció.
        debug = []
        if gd_raw.strip():
            debug.append(f"gallery-dl stdout[:2500]={gd_raw[:2500]!r}")
        if gd_err:
            debug.append(f"gallery-dl stderr[:200]={gd_err[:200]!r}")
        if yd_raw.strip():
            debug.append(f"yt-dlp stdout[:200]={yd_raw[:200]!r}")
        if debug:
            detail += " || DEBUG: " + " | ".join(debug)
        # Pista de autenticación: Instagram suele requerir cookies/login.
        if any(k in detail.lower() for k in ("login", "cookie", "401", "unauthorized", "private")):
            detail += " → prueba configurando COOKIES_FILE (cookies Netscape de tu navegador)."
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
    except Exception as e:  # seguridad: captura final ante fallos imprevistos
        raise HTTPException(status_code=500, detail=f"Error inesperado: {e}")

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
    text = raw.decode("utf-8", "replace")
    # Validación mínima de formato Netscape: líneas con >=7 campos separados por tab.
    domains: set[str] = set()
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 7:
            domains.add(parts[0].lstrip("."))
    if not domains:
        raise HTTPException(status_code=400, detail="No parece un cookies.txt Netscape válido.")
    os.makedirs(os.path.dirname(os.path.abspath(COOKIES_FILE)), exist_ok=True)
    tmp = COOKIES_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, COOKIES_FILE)  # escritura atómica
        print(f"[cookies] guardado {COOKIES_FILE}: {len(raw)} bytes, domains={sorted(domains)}",
              file=sys.stderr)
    except OSError as e:
        raise HTTPException(
            status_code=403,
            detail=f"No se pudo escribir el cookies.txt (¿montado read-only?): {e}",
        )
    return {"configured": True, "bytes": len(raw), "domains": sorted(domains)}


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
        headers["Range"] = range_hdr

    client = httpx.AsyncClient(timeout=httpx.Timeout(PROXY_TIMEOUT, connect=15.0), follow_redirects=True)
    req = client.build_request("GET", url, headers=headers)
    try:
        resp = await client.send(req, stream=True)
    except httpx.RequestError as e:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Error de red con el origen: {e}")

    if resp.status_code >= 400:
        body = await resp.aread()
        await resp.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"Origen devolvió {resp.status_code}: "
                   f"{body[:200].decode('utf-8', 'replace')}",
        )

    # Content-Type: preferimos el del origen; si no, inferimos de la URL.
    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
    if not ctype:
        ctype = (mimetypes.guess_type(parsed.path)[0]
                 or mimetypes.guess_type(fname)[0]
                 or "application/octet-stream")

    if inline:
        disposition = "inline"
        cache = "public, max-age=3600"
    else:
        disposition = "attachment; filename=\"{}\"; filename*=UTF-8''{}".format(
            fname.replace('"', ""), quote(fname)
        )
        cache = "no-store"

    async def gen() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE):
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
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)