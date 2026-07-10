# PureStream-DL: Instagram session invalidation problem

## Contexto del proyecto

**Repositorio:** https://github.com/samuelrubiodev/PureStream-DL

App self-hosted (FastAPI + gallery-dl + yt-dlp) que extrae metadatos multimedia y los streamea al navegador del usuario vía proxy en RAM. Cero almacenamiento de media en disco. Desplegada con Docker Compose en Dokploy (servidor local, no cloud).

Estado actual: Twitter funciona perfectamente. Instagram **dejó de funcionar repentinamente** hace un día sin causa evidente. El problema se reproduce con la cuenta A pero **NO con la cuenta B** en el mismo servidor y mismas herramientas.

## El problema exacto

Síntoma: tras una actualización de Dokploy (que probablemente recreó el contenedor), Instagram empezó a rechazar sistemáticamente la sesión de la cuenta A.

Log real:
```
[extract] gallery-dl: dicts=0 errs=1 stdout_bytes=141 stderr_bytes=0
err_preview='HTTP redirect to home page (https://www.instagram.com/)'

[gallery-dl][debug] https://www.instagram.com:443
"GET /api/v1/media/3936241542110452058/info/ HTTP/1.1" 302 0
[gallery-dl][debug] https://www.instagram.com:443 "GET / HTTP/1.1" 200 None
```

Diagnóstico que ya hemos confirmado:
- Cookies válidas (funcionan en el navegador, mismas claves: `sessionid`, `ds_user_id`, `csrftoken`, `mid`, `ig_did`, `datr`, `rur`).
- User-Agent correcto (el mismo que el navegador).
- IP no está bloqueada (otra cuenta funciona desde la misma IP).
- No es suspensión, 2FA, ni bloqueo de cuenta.
- `docker compose build --no-cache` con `pip install --upgrade gallery-dl yt-dlp` no lo arregla.
- Probar con Cloudflare Warp en el PC del usuario tampoco arregla la cuenta A.

**Conclusión actual**: la cuenta A tiene su sesión de Instagram vinculada a un fingerprint/contexto del entorno anterior del contenedor (probablemente el `sessionid` fue invalidado por Instagram al detectar el cambio, y la cuenta requiere "verificación de dispositivo" que gallery-dl/yt-dlp no pueden hacer). El navegador sí puede renovar la sesión automáticamente, pero gallery-dl/yt-dlp no.

## Por qué ocurre

`gallery-dl` y `yt-dlp` usan el `sessionid` directamente contra la API interna de Instagram. Esta API detecta "este sessionid nunca ha usado este User-Agent/este contexto" y:
- Para un subconjunto de cuentas (probablemente las que tienen login challenges pendientes, 2FA reciente, o cambios de dispositivo marcados) → rechaza con `HTTP redirect to login page`.
- Para la mayoría de cuentas (caso de la cuenta B) → funciona.

Instagram vincula cada `sessionid` a:
- IP de origen
- User-Agent
- Fingerprint del navegador (cuando lo creaste)
- Token `datr` (device token, dura ~2 años)

Si Docker se recrea y el `datr` cambia (raro) o el contexto de la petición varía ligeramente (por ejemplo porque Python/httpx añaden headers por defecto distintos a los de un navegador), Instagram puede tratar esa sesión como robada.

## Lo que ya probamos y NO funciona

1. Cookies frescas, exportadas correctamente en formato Netscape.
2. `GALLERY_DL_UA` con el UA exacto del navegador.
3. Volume persistente `./data:/data` con el cookies.txt.
4. `docker compose build --no-cache` para forzar upgrade de gallery-dl/yt-dlp.
5. `gallery-dl --dump-json -o api=graphql` (extractor legacy).
6. `gallery-dl --dump-json` sin cookies (post público) → bloqueado también.
7. yt-dlp como fallback.
8. Probar con Cloudflare Warp en el PC (descartado IP).

## Lo que se quiere conseguir

**Una solución que prevenga esto de forma permanente** y que, en el peor caso, permita a la app **regenerar o reemplazar la sesión automáticamente** sin intervención manual del usuario, o al menos detecte la invalidez y avise claramente.

## Opciones a evaluar

### Opción A — Browser headless con Playwright (FAVORITA del usuario)

**Idea**: usar Playwright/Chromium headless para abrir Instagram en el navegador, hacer login automáticamente, dejar que Instagram haga su handshake/renovación de tokens, y entonces capturar las cookies actualizadas. Después pasar esas cookies a gallery-dl/yt-dlp.

**Ventajas**:
- Es exactamente lo que haría un usuario real: loguearse en un navegador.
- Bypassea las detecciones porque usa un Chromium real, no httpx/requests.
- Permite resolver challenges visuales, 2FA, etc. (si surgen).
- Reutiliza la sesión en lugar de empezar de cero cada vez.

**Problemas**:
- Playwright pesa ~150-300 MB (Chromium).
- Headless detectable: Instagram detecta navegadores headless y a veces bloquea. Hay que usar `headless="new"` (Chromium real) y ocultar webdriver con `playwright-stealth` o equivalente.
- 2FA: si el usuario tiene 2FA activo, hay que pausar e introducir el código manualmente. Esto se puede hacer exponiendo una UI en la web ("necesito que introduzcas el código que Instagram te acaba de enviar") y guardando el `playwright_state.json` (cookies + localStorage) cifrado en disco.
- Mantenimiento: si Instagram cambia el flujo de login, hay que ajustar el script.

**Cómo se integraría**:
1. Nuevo endpoint `POST /api/auth/instagram` con credenciales username/password.
2. Backend lanza Playwright headless, va a instagram.com, hace login.
3. Si pide 2FA: pide código vía WebSocket o polling con nuevo endpoint.
4. Al final, extrae las cookies actualizadas y las guarda en `COOKIES_FILE` con permisos 0600.
5. La extracción normal usa esas cookies renovadas.

### Opción B — Refresco automático de cookies vía sesión de navegador en background

**Idea**: levantar un Chromium headless **siempre corriendo** dentro del contenedor, con un perfil de Instagram logueado, y que periódicamente extraiga cookies. O un navegador "standby" que solo se activa cuando se detecta un fallo de extracción.

**Problemas**: misma que A pero más complejo (mantener proceso vivo, restaurar sesión tras reinicios, etc.).

### Opción C — Cookies de larga duración sin navegador

**Idea**: usar **cookiegen** o **instagrapi** (librería Python que simula un cliente privado de Instagram y maneja su propio login). Una vez logueado, genera un `sessionid` de larga duración.

**Problemas**:
- `instagrapi` no es oficial y Meta lo detecta/bloquea con más facilidad.
- Sigue sin resolver el problema de fondo (Instagram invalidando sesiones).

### Opción D — Renovar cookies a demanda cuando fallan

**Idea**: cuando la extracción falle con `redirect to login`, automáticamente:
1. Pedir al usuario sus credenciales por una UI ("Tu sesión ha caducado, vuelve a iniciar sesión").
2. Usar Playwright (Opción A) para regenerar cookies.
3. Reintentar la extracción.

**Ventajas**: la más realista a corto plazo, no necesita un Chromium corriendo siempre.

## Recomendación: combinación de A + D

1. Implementar un endpoint `POST /api/auth/login` que abra Playwright headless, pida credenciales y, si hace falta, pida código 2FA en una segunda llamada.
2. Guardar el estado de Playwright (`storage_state`) en `/data/playwright_state.json` con permisos 0600.
3. Al arrancar el backend, si existe ese estado, restaurarlo (sin pasar por login).
4. Si una extracción falla con `redirect to login`, intentar refrescar la sesión vía Playwright (abrir el navegador, ir a instagram.com, si está logueado reextraer cookies, si no re-pedir login) y reintentar la extracción una vez.
5. Si la renovación falla, devolver al usuario un mensaje claro: "Instagram rechazó la sesión; ve a 🍪 y vuelve a iniciar sesión desde el navegador".

## Detalles técnicos para Playwright

```python
# Estructura básica del flujo de login
from playwright.async_api import async_playwright

async def refresh_instagram_session():
    async with async_playwright() as p:
        # NO usar --headless (o usar --headless=new en Chromium reciente)
        # para evitar detección de "headless browser".
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled']
        )
        context = await browser.new_context(
            storage_state=STATE_FILE if os.path.exists(STATE_FILE) else None,
            user_agent=GALLERY_DL_UA or "Mozilla/5.0 (X11; Linux x86_64) ..."
        )
        page = await context.new_page()
        await page.goto("https://www.instagram.com/")
        if await page.locator("text=Log in").is_visible():
            # Necesita login: pedir credenciales al usuario
            return {"needs_login": True}
        # Ya logueado: extraer cookies
        cookies = await context.cookies()
        await browser.close()
        return {"cookies": cookies, "needs_login": False}
```

**Anti-detección**:
- `playwright-stealth` o `undetected-playwright` (fork que aplica parches de stealth).
- User-Agent real.
- Viewport realista (1920x1080).
- No usar el flag `--enable-automation`.
- Considerar usar `xvfb` si hace falta un "entorno gráfico" para que Chromium no actúe en modo "headless" detectable.

**2FA**:
- Si Instagram pide código, Playwright detecta el input `#[name="verificationCode"]`.
- Backend expone endpoint `POST /api/auth/2fa` para que el frontend envíe el código.
- Estado de Playwright en memoria mientras se espera.

## Estado actual del repositorio

- Código principal: `main.py` (FastAPI, ~700 líneas).
- Frontend: `templates/index.html` (PWA, Tailwind compilado en el Dockerfile).
- Tests: `test_parse.py` (self-check de parsers, sin red).
- Dockerfile: Python 3.11-slim + ffmpeg + gallery-dl + yt-dlp + Tailwind CLI.
- Endpoints actuales:
  - `GET /` → SPA.
  - `POST /api/extract` → extrae metadatos.
  - `GET /api/proxy` → stream de media.
  - `GET/POST /api/cookies` → gestión de cookies.
  - `GET /api/health` → versión y estado.

## Comandos útiles para el siguiente agente

```bash
# Ver logs en vivo
docker compose logs -f media-downloader

# Probar gallery-dl manualmente con las cookies actuales
docker compose run --rm media-downloader \
  gallery-dl --dump-json -v --cookies /data/cookies.txt \
  -o user-agent="<UA>" "https://www.instagram.com/p/<shortcode>/"

# Confirmar versión de gallery-dl/yt-dlp en el contenedor
docker compose run --rm media-downloader gallery-dl --version
docker compose run --rm media-downloader yt-dlp --version

# Confirmar IP del servidor
docker compose run --rm media-downloader curl -s ifconfig.me
```

## Punto importante a NO olvidar

- **El usuario ha sido claro**: no quiere más conjeturas tipo "es la IP" o "2FA". El problema es que gallery-dl/yt-dlp no pueden refrescar una sesión invalidada de Instagram. La solución debe ser **automática y transparente**, no pedirle al usuario que reinicie sesión manualmente cada vez.
- La experiencia de uso debe ser: en el peor caso, la app detecta el problema, abre un Chromium headless, refresca la sesión silenciosamente, y completa la descarga. Si hace falta 2FA, se le pide al usuario **una vez** el código, pero no debería tener que re-exportar cookies nunca más.
- El código actual ya está razonablemente limpio y los bugs de seguridad/robustez están arreglados (commit `5a58c4b` y siguientes). No tocar la lógica de extracción a menos que sea para integrar el refresh.

## Variables de entorno a añadir (si se implementa)

```
INSTAGRAM_USERNAME=
INSTAGRAM_PASSWORD=
# Opcional, para Playwright:
PLAYWRIGHT_HEADLESS=new
PLAYWRIGHT_BROWSER=chromium
# Estado de Playwright (cookies + localStorage) se guarda aquí:
PLAYWRIGHT_STATE_FILE=/data/playwright_state.json
```

## Conclusión para el siguiente agente

Implementa la Opción A+D con Playwright. Es lo que el usuario quiere y lo que cierra este problema definitivamente. El resto de la app ya está robusta; solo hay que añadir el sistema de refresh de sesión para Instagram.
