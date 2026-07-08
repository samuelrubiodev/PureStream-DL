# PureStream-DL

Aplicación web **self-hosted** para descargar multimedia (imágenes, carruseles, GIFs y vídeos) de redes sociales y cientos de sitios soportados por **gallery-dl** y **yt-dlp**.

> **Restricción clave: cero almacenamiento en servidor.** El servidor actúa solo como puente en RAM: extrae metadatos y retransmite los bytes del CDN de origen al navegador del dispositivo del usuario. **Ningún archivo multimedia se escribe en el disco del servidor.** La descarga la gestiona el navegador del cliente en su almacenamiento local.

Mobile-first, responsive, modo oscuro por defecto, instalable como **PWA** (Android/PC). Pensada para desplegar en un homelab con Docker.

---

## Características

- **Extracción sin descarga**: usa `gallery-dl --dump-json` (y `yt-dlp` como fallback) para obtener solo metadatos.
- **Grid de previsualización** con checkboxes: elige qué elementos de un carrusel descargar.
- **Preview real**: imágenes vía poster; **vídeos sin miniatura muestran un frame real** (`<video preload=metadata>` + proxy con passthrough de `Range`).
- **Proxy de streaming**: retransmite el medio desde el CDN de origen al navegador con `Content-Disposition: attachment`. Sin tocar disco.
- **Multi-sitio**: gallery-dl/yt-dlp soportan cientos de sitios (Twitter/X, Instagram, Reddit, Pixiv, TikTok, …). El proxy permite cualquier CDN **público** (anti-SSRF: bloquea IPs privadas/loopback/link-local).
- **Cookies universales**: un único `cookies.txt` (Netscape, multi-dominio) sirve para todos los sitios que requieran sesión. Server-side → compartidas por todos tus dispositivos.
- **Dos formas de proveer cookies**: subida desde la web (botón 🍪) o montaje del fichero vía `docker-compose` (read-only).
- **Robusto**: errores de herramientas (URLs privadas, formatos inválidos, login requerido) se muestran limpiamente en la UI; la app no se congela.
- **Diagnóstico integrado**: logs `[extract]` en el contenedor y mensajes de error descriptivos.

---

## Arquitectura

```
Usuario (navegador)
   │  1. POST /api/extract  {url}
   ▼
Backend (FastAPI, RAM)
   │  2. gallery-dl --dump-json  →  (fallback) yt-dlp --dump-json
   │  3. parsea JSON → array de medios {url, thumbnail, width, height, type, ...}
   ▼
Frontend (grid con checkboxes + previews)
   │  4. usuario selecciona y pulsa "Descargar"
   ▼
   │  5. GET /api/proxy?url=...&filename=...
   ▼
Backend → CDN de origen (stream=True) → StreamingResponse (attachment) → navegador
```

- **Backend**: Python 3.11 + FastAPI + httpx. `StreamingResponse` asíncrono.
- **Frontend**: HTML5 + JS (Fetch API, DOM asíncrono) + **Tailwind CSS compilado** (CLI standalone, autohospedado, sin CDN) + PWA (manifest + service worker).
- **Herramientas**: `gallery-dl`, `yt-dlp`, `ffmpeg`.

---

## Estructura

```
main.py                 Backend FastAPI: /api/extract, /api/proxy, /api/cookies, /api/health
templates/index.html    Frontend (grid, checkboxes, previews, descarga, PWA)
src/input.css           Entrada Tailwind
tailwind.config.js      Config Tailwind (darkMode: class)
static/                 manifest.json, sw.js, icon.svg  (tailwind.css se genera en el build)
test_parse.py           Self-check de parseadores (sin red)
Dockerfile             python:3.11-slim + ffmpeg + gallery-dl + yt-dlp + Tailwind CLI
docker-compose.yml      Despliegue homelab (puerto 8000, volumen data, cookies)
requirements.txt        fastapi, uvicorn, httpx, python-multipart
```

---

## Despliegue (Docker)

```bash
git clone https://github.com/samuelrubiodev/PureStream-DL.git
cd PureStream-DL
docker compose up -d --build
```

Abre **http://localhost:8000** o la dirección IP/LAN del servidor donde se ejecute.

Verifica la versión corriendo:
```bash
curl -s localhost:8000/api/health
# {"status":"ok","version":"1.3.2","cookies":...,"gallery_dl_ua":...}
```

> El CSS de Tailwind **se compila durante el build de Docker** (no se sirve desde un CDN). Si ejecutas sin Docker, genera los assets una vez:
> ```bash
> ./tailwindcss -i src/input.css -o static/tailwind.css --minify
> python3 static/gen_icons.py
> ```

---

## Cookies (Instagram y sitios con sesión)

Instagram (y muchos sitios) exigen sesión. Un **único `cookies.txt`** en formato Netscape, con cookies de **varios dominios**, sirve para todos a la vez. Las cookies viven en el **servidor**, así que todos los dispositivos conectados (Android, PC, etc.) las comparten: se configuran una sola vez.

### Opción A — Subir desde la web
1. Exportar `cookies.txt` (Netscape) desde una sesión iniciada en el navegador:
   - Chrome/Edge: extensión **"Get cookies.txt LOCALLY"**.
   - Firefox: **"Export Cookies"**.
   - Estando logueado en el sitio (instagram.com, x.com, …) → exportar.
2. En la app, pulsar el botón **🍪** arriba a la derecha y subir el fichero. El punto se pondrá verde y el tooltip mostrará los dominios detectados.

### Opción B — Montar vía docker-compose (read-only)
1. Colocar el `cookies.txt` junto al `docker-compose.yml`.
2. Descomentar la línea del volumen:
   ```yaml
   volumes:
     - ./data:/data
     - ./cookies.txt:/data/cookies.txt:ro
   ```
3. Ejecutar `docker compose up -d --build`. El botón 🍪 indicará read-only. Para actualizar: editar el fichero en el host y reiniciar el contenedor.

> ⚠️ **Seguridad**: el `cookies.txt` concede acceso total a las cuentas de las sesiones exportadas. **No debe compartirse ni subirse a git** (ya está en `.gitignore`). Refrescar las cookies si caducan (Instagram expira `sessionid` rápido). Si Instagram sigue redirigiendo a login con cookies válidas, probar fijando `GALLERY_DL_UA` al User-Agent del navegador usado para exportarlas (descomentar en `docker-compose.yml`); a veces Instagram rechaza por mismatch de UA, o banea la IP (cambiar de red/VPN).

---

## Instalación como aplicación (PWA)

PureStream-DL incluye los componentes necesarios para instalarse como aplicación web progresiva: `manifest.json`, Service Worker, iconos PNG 192x192 y 512x512, y tema/soporte para modo oscuro.

**Requisito importante**: los navegadores modernos, incluido Chrome en Android, solo ofrecen la opción de "Añadir a pantalla principal" o "Instalar aplicación" cuando la app se sirve a través de **HTTPS** (o `localhost` en entornos locales). Si se accede por HTTP en una red local o dominio sin certificado, el prompt de instalación no aparecerá aunque todos los archivos PWA estén correctos.

Para habilitar la instalación en Android u otros dispositivos, configure el despliegue detrás de un proxy inverso con TLS (por ejemplo, Caddy, Traefik o nginx con un certificado de Let's Encrypt), o utilice una solución de túnel privado que proporcione HTTPS confiable.

---

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `COOKIES_FILE` | (none) | Ruta del cookies.txt. Ej: `/data/cookies.txt`. |
| `GALLERY_DL_UA` | (none) | User-Agent pasado a gallery-dl (mismatch con cookies → login reject). |
| `EXTRACT_TIMEOUT` | `60` | Segundos máx. de ejecución de gallery-dl/yt-dlp. |
| `PROXY_TIMEOUT` | `300` | Segundos máx. de inactividad del proxy de streaming. |

---

## Seguridad

- **Cero almacenamiento de media en servidor**: solo proxy en RAM.
- **Anti-SSRF** en el proxy: lista blanca de CDN de Twitter/Instagram sin resolución; cualquier otro host se resuelve y se rechaza si apunta a una IP **privada/loopback/link-local/reservada** (p. ej. `169.254.169.169`). Así no se puede abusar del proxy para reached servicios internos.
- **`cookies.txt`**: auth/config (no media), se guarda en disco solo porque las herramientas lo exigen. Se excluye de git.
- El proxy envía User-Agent realista y **sin Referer** para evitar hotlink-403 de los CDN.

---

## Solución de problemas

- **`No se encontró multimedia` / login redirect en Instagram**: añadir/proporcionar cookies válidas (ver arriba).
- **Preview gris en vídeos de Twitter**: normal si gallery-dl no aporta poster; la app muestra un frame real del mp4. Si no aparece, puede ser mp4 sin faststart.
- **403 al descargar**: re-extrae (las URLs del CDN caducan). El proxy devuelve 502 con el código del origen si falla.
- **`dicts=0` en logs**: pega `docker compose logs media-downloader | grep '\[extract\]'` y el mensaje de error de la web para diagnosticar.
- **Actualizar herramientas**: `docker compose build --no-cache` reinstala la última versión de gallery-dl/yt-dlp.

---

## Desarrollo

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt gallery-dl yt-dlp
python test_parse.py          # self-check de parseadores (sin red)
uvicorn main:app --reload    # necesita static/tailwind.css (genera con Tailwind CLI)
```

## Licencia

MIT.