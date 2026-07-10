"""
ig_auth.py — Refresco automático de la sesión de Instagram vía Playwright.

Por qué existe
--------------
gallery-dl/yt-dlp usan el `sessionid` directamente contra la API interna de
Instagram. Esa API detecta un fingerprint "no-navegador" (httpx/requests) y, para
un subconjunto de cuentas, invalida la sesión con `HTTP redirect to login`. Un
Chromium REAL (Playwright) hace el handshake de dispositivo que haría un
navegador, manteniendo la sesión viva: abrimos instagram.com en Chromium headless,
dejamos que renueve los tokens y re-exportamos las cookies a COOKIES_FILE
(formato Netscape) para que las reutilice gallery-dl.

Flujos
-------
1. **Refresco silencioso (común, sin password)**: reutiliza el `storage_state`
   persistido (cookies + localStorage de Chromium), abre IG, confirma que sigue
   logueado y re-exporta cookies. Asígallery-dl vuelve a funcionar sin pedir nada
   al usuario.
2. **Login con credenciales (env INSTAGRAM_USERNAME/PASSWORD)**: si la sesión
   murió del todo y hay creds en env, hace login headless. Si IG pide 2FA, el
   frontend recoge el código UNA vez y lo envía a /api/auth/instagram/2fa.
3. **Bootstrap**: si no hay storage_state pero sí cookies.txt, importa esas
   cookies al contexto de Playwright antes de la primera visita (así el state
   se crea desde las cookies que el usuario ya exportó de su navegador).

Seguridad
---------
- NUNCA se persiste la password. Solo `storage_state` y `cookies.txt`, que ya
  viven en el servidor con 0600 (mismo nivel de acceso que las cookies que el
  usuario ya sube). Ver INSTAGRAM_SESSION_REFRESH.md.
- Las credenciales viajan por HTTP si la app no está tras HTTPS/localhost; el
  usuario ya acepta lo mismo con la subida de cookies.

El módulo NO importa Playwright en el top-level: solo lo importa bajo demanda
dentro de las funciones. Así, si Playwright no está instalado, la app sigue
funcionando con cookies manuales (comportamiento previo) y estos endpoints
devuelven 503 / se saltan el auto-refresh.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

# ------------------------------------------------------------------------- #
# Configuración (env; leída al importar)
# ------------------------------------------------------------------------- #

# storage_state de Playwright (cookies + localStorage). Persiste entre reinicios
# vía el volumen ./data:/data. Mismo nivel de sensibilidad que cookies.txt.
STATE_FILE = os.getenv("PLAYWRIGHT_STATE_FILE", "/data/playwright_state.json")

# Credenciales opcionales para el login silencioso cuando la sesión muere.
# Si NO están fijadas, el login se pide vía UI (una vez) y no se persiste.
IG_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
IG_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")

# UA de Chromium: si GALLERY_DL_UA está fijado, úsalo (debe cuadrar con las
# cookies); si no, un Chrome realista de Linux. Importado aquí para no acoplar
# este módulo al global de main.py.
GALLERY_DL_UA = os.getenv("GALLERY_DL_UA", "")
CHROME_UA = GALLERY_DL_UA or (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Cooldown entre refrescos automáticos (seg). Evita lanzar Chromium en cada
# extract fallido seguido; un refresco mantiene la sesión unos minutos.
REFRESH_COOLDOWN = int(os.getenv("IG_REFRESH_COOLDOWN", "300"))
# Timeouts del navegador (seg).
NAV_TIMEOUT = int(os.getenv("IG_NAV_TIMEOUT", "45"))      # navegación / selectores
LOGIN_WAIT = int(os.getenv("IG_LOGIN_WAIT", "180"))       # esperar código 2FA del usuario
SESSION_TTL = int(os.getenv("IG_SESSION_TTL", "900"))     # vida máx de una sesión de login

# Marcas que delatan "sesión rechazada" en los errores de gallery-dl/yt-dlp.
# extract_media las usa para decidir si intentar el auto-refresh.
LOGIN_REDIRECT_MARKERS = (
    "redirect to home",
    "redirect to login",
    "login required",
    "login page",
)

# Último refresco realizado (epoch seg). Cooldown global para el auto-refresh.
_last_refresh = 0.0


def playwright_available() -> bool:
    """True si el paquete Playwright está instalado (no implica que Chromium lo
    esté; eso se averigua al lanzar)."""
    try:
        import playwright  # noqa: F401
    except Exception:
        return False
    return True


def env_creds_set() -> bool:
    return bool(IG_USERNAME and IG_PASSWORD)


def state_exists() -> bool:
    return bool(STATE_FILE) and os.path.isfile(STATE_FILE)


def refresh_off_cooldown() -> bool:
    """True si ha pasado el cooldown desde el último refresco (o nunca se hizo)."""
    return (time.time() - _last_refresh) >= REFRESH_COOLDOWN


# ------------------------------------------------------------------------- #
# Conversión de cookies: Netscape (cookies.txt) <-> Playwright
# ------------------------------------------------------------------------- #

def _parse_netscape_line(line: str) -> dict[str, Any] | None:
    """Una línea de cookies.txt -> dict de cookie de Playwright, o None."""
    line = line.rstrip("\n")
    if not line.strip() or line.startswith("#") and not line.startswith("#HttpOnly_"):
        return None
    # HttpOnly se codifica como prefijo "#HttpOnly_" en el campo dominio.
    http_only = False
    if line.startswith("#HttpOnly_"):
        http_only = True
        line = line[len("#HttpOnly_"):]
    parts = line.split("\t")
    if len(parts) < 7:
        return None
    domain, flag, path, secure, expires, name, value = parts[:7]
    if not name or not domain:
        return None
    flag_bool = flag.strip().upper() == "TRUE"
    # flag TRUE => cookie de dominio (subdominios): Playwright quiere punto inicial.
    if flag_bool and not domain.startswith("."):
        domain = "." + domain
    elif not flag_bool and domain.startswith("."):
        domain = domain.lstrip(".")
    try:
        exp = int(expires)
    except ValueError:
        exp = 0
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path or "/",
        "secure": secure.strip().upper() == "TRUE",
        "httpOnly": http_only,
        "sameSite": "Lax",
        "expires": -1 if exp <= 0 else exp,
    }


def cookies_to_playwright(netscape_path: str) -> list[dict[str, Any]]:
    """Lee un cookies.txt Netscape y devuelve cookies para Playwright.
    Deduplica por (domain, path, name) quedándose con la mayor expiración, para
    no mezclar cookies viejas con nuevas."""
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    try:
        with open(netscape_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                c = _parse_netscape_line(line)
                if not c:
                    continue
                key = (c["domain"], c["path"], c["name"])
                prev = latest.get(key)
                if prev is None or c["expires"] > prev["expires"]:
                    latest[key] = c
    except OSError:
        return []
    return list(latest.values())


def _playwright_cookie_to_netscape(c: dict[str, Any]) -> str:
    domain = c.get("domain", "")
    flag = "TRUE" if domain.startswith(".") else "FALSE"
    secure = "TRUE" if c.get("secure") else "FALSE"
    exp = int(c.get("expires", -1))
    exp_str = str(exp) if exp > 0 else "0"
    path = c.get("path", "/")
    return "\t".join([
        domain, flag, path, secure, exp_str, c.get("name", ""), c.get("value", "")
    ])


def write_netscape(cookies: list[dict[str, Any]], path: str) -> None:
    """Escribe cookies de Playwright a un cookies.txt Netscape (0600). Escritura
    atómica via tmp+replace para no dejar un fichero a medio escribir."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    lines = ["# Netscape HTTP Cookie File",
             "# Generated by PureStream-DL ig_auth (Playwright session refresh)."]
    # Orden estable por dominio+nombre para diffs legibles.
    for c in sorted(cookies, key=lambda x: (x.get("domain", ""), x.get("name", ""))):
        lines.append(_playwright_cookie_to_netscape(c))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


# ------------------------------------------------------------------------- #
# Playwright: lanzamiento, stealth, detección de estado
# ------------------------------------------------------------------------- #

# Parches de stealth mínimos (sin dep de playwright-stealth). Cubren los
# "tells" básicos de headless que Instagram comprueba. Si Instagram sigue
# detectando, escalar a xvfb en modo headed (no implementado aquí).
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
const _q = window.navigator.permissions && window.navigator.permissions.query;
if (_q) {
  window.navigator.permissions.query = (p) => p && p.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission }) : _q(p);
}
"""

# Args de Chromium para Docker: /dev/shm pequeño y sandbox necesita no-root.
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-gpu",
]

# Selectores del flujo de login de Instagram. FRÁGILES: IG cambia el DOM a
# menudo. Centralizados aquí para tocarlos en un solo sitio si cambian.
SEL_USERNAME = 'input[name="username"]'
SEL_PASSWORD = 'input[name="password"]'
SEL_SUBMIT = 'button[type="submit"]'
SEL_2FA_CODE = 'input[name="verificationCode"], input[autocomplete="one-time-code"], input[name="confirmationCode"]'


async def _launch(p):
    return await p.chromium.launch(headless=True, args=_LAUNCH_ARGS)


async def _new_context(browser, state_path: str | None):
    state = state_path if (state_path and os.path.isfile(state_path)) else None
    ctx = await browser.new_context(
        storage_state=state,
        user_agent=CHROME_UA,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
    )
    await ctx.add_init_script(_STEALTH_JS)
    return ctx


async def _selector_visible(page, selector: str, timeout_ms: int = 1500) -> bool:
    try:
        await page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


async def _is_logged_in(page) -> bool:
    """Tras cargar instagram.com: logueado si la URL NO es de login y no hay
    formulario de login visible."""
    cur = page.url.lower()
    if "accounts/login" in cur or "/login" in cur:
        return False
    if await _selector_visible(page, SEL_USERNAME, timeout_ms=1200):
        return False
    return True


async def _login_error_visible(page) -> bool:
    """Detecta un mensaje de error de login (creds rechazadas / cuenta
    deshabilitada / checkpoint). Best-effort: selectores variables."""
    for sel in ('#slfErrorAlert', 'div[role="alert"]', 'span[class*="error"]'):
        try:
            el = await page.query_selector(sel)
            if el:
                txt = (await el.inner_text()).lower()
                if any(k in txt for k in ("incorrect", "sorry", "disabled",
                                         "locked", "challenge", "suspicious")):
                    return True
        except Exception:
            continue
    return False


async def _fill_login(page, username: str, password: str) -> None:
    await page.wait_for_selector(SEL_USERNAME, timeout=NAV_TIMEOUT * 1000)
    await page.fill(SEL_USERNAME, username)
    await page.fill(SEL_PASSWORD, password)
    await page.click(SEL_SUBMIT)


async def _finish(ctx, cookies_file: str) -> dict[str, Any]:
    """Persiste cookies (Netscape) + storage_state y marca el refresco hecho."""
    global _last_refresh
    cookies = await ctx.cookies()
    if cookies_file:
        write_netscape(cookies, cookies_file)
    if STATE_FILE:
        await ctx.storage_state(path=STATE_FILE)
        os.chmod(STATE_FILE, 0o600)
    _last_refresh = time.time()
    return {"status": "ok", "cookies": len(cookies)}


async def _resolve_login(page, ctx, cookies_file: str,
                         twoFA_future: "asyncio.Future[str] | None" = None,
                         session: "LoginSession | None" = None) -> dict[str, Any]:
    """
    Tras enviar creds, decide el resultado: logged in / 2FA / error.
    - twoFA_future=None (refresco silencioso): si aparece 2FA, devuelve
      needs_2fa SIN esperar (no hay quien meta el código).
    - twoFA_future provisto (login interactivo): espera el código del usuario
      via el future, lo introduce y confirma el login.
    `session` (opcional) sirve para que el frontend vea el estado por polling.
    """
    deadline = time.time() + NAV_TIMEOUT
    while time.time() < deadline:
        await page.wait_for_timeout(800)
        # ¿Pide 2FA / código de verificación?
        if "two_factor" in page.url.lower() or await _selector_visible(page, SEL_2FA_CODE, 1200):
            if session:
                session.status = "needs_2fa"
            if twoFA_future is None:
                # Silencioso: no podemos resolver el 2FA aquí.
                return {"status": "needs_2fa",
                        "error": "Instagram exige verificación 2FA. "
                                 "Pulsa 'Renovar sesión' en la web e introduce el código."}
            # Interactivo: esperar el código del usuario.
            if session:
                session.status = "needs_2fa"
            try:
                code = await asyncio.wait_for(asyncio.shield(twoFA_future), timeout=LOGIN_WAIT)
            except asyncio.TimeoutError:
                return {"status": "error", "error": "2FA: tiempo de espera agotado."}
            except asyncio.CancelledError:
                return {"status": "error", "error": "2FA: sesión cancelada."}
            await page.fill(SEL_2FA_CODE, code)
            await page.click(SEL_SUBMIT)
            # Confirmar login tras el 2FA.
            ok_dl = time.time() + NAV_TIMEOUT
            while time.time() < ok_dl:
                await page.wait_for_timeout(800)
                if await _is_logged_in(page):
                    return await _finish(ctx, cookies_file)
                if await _login_error_visible(page):
                    return {"status": "error", "error": "Código 2FA rechazado."}
            return {"status": "error", "error": "2FA: no se confirmó el login."}
        if await _is_logged_in(page):
            return await _finish(ctx, cookies_file)
        if await _login_error_visible(page):
            return {"status": "error",
                    "error": "Credenciales rechazadas o login bloqueado por Instagram."}
    return {"status": "error", "error": "Login: Instagram no respondió a tiempo."}


# ------------------------------------------------------------------------- #
# Refresco silencioso (auto-refresh en extract_media / botón manual)
# ------------------------------------------------------------------------- #

async def refresh_session_silent(cookies_file: str) -> dict[str, Any]:
    """
    Refresco SIN interacción del usuario. Devuelve:
      {"status":"ok","cookies":N}                 -> cookies re-exportadas
      {"status":"needs_login","error":...}        -> sesión muerta, sin creds
      {"status":"needs_2fa","error":...}           -> IG pide 2FA (no resoluble aquí)
      {"status":"error","error":...}               -> fallo de Playwright / IO
    No lanza: el llamador decide qué hacer según el status.
    """
    if not playwright_available():
        return {"status": "error", "error": "Playwright no instalado en este contenedor."}
    if cookies_file and not os.access(
            os.path.dirname(os.path.abspath(cookies_file)) or ".", os.W_OK):
        return {"status": "error", "error": "COOKIES_FILE no es escribible (montado read-only)."}
    from playwright.async_api import async_playwright
    try:
        async with async_playwright() as p:
            browser = await _launch(p)
            try:
                ctx = await _new_context(browser, STATE_FILE)
                # Bootstrap: si no hay storage_state pero sí cookies.txt,
                # importa esas cookies al contexto antes de la primera visita.
                if not state_exists() and cookies_file and os.path.isfile(cookies_file):
                    cks = cookies_to_playwright(cookies_file)
                    if cks:
                        await ctx.add_cookies(cks)
                page = await ctx.new_page()
                await page.goto("https://www.instagram.com/",
                                wait_until="domcontentloaded", timeout=NAV_TIMEOUT * 1000)
                await page.wait_for_timeout(2500)  # dejar que IG asiente/renueve
                if await _is_logged_in(page):
                    return await _finish(ctx, cookies_file)
                # Sesión muerta. ¿Creds en env para login silencioso (sin 2FA)?
                if env_creds_set():
                    if "login" not in page.url.lower():
                        await page.goto("https://www.instagram.com/accounts/login/",
                                        wait_until="domcontentloaded", timeout=NAV_TIMEOUT * 1000)
                    await _fill_login(page, IG_USERNAME, IG_PASSWORD)
                    # twoFA_future=None -> si aparece 2FA, devuelve needs_2fa.
                    return await _resolve_login(page, ctx, cookies_file,
                                                twoFA_future=None, session=None)
                return {"status": "needs_login",
                        "error": "Sesión de Instagram caducada y no hay "
                                 "INSTAGRAM_USERNAME/PASSWORD. Inicia sesión desde la web (🔑)."}
            finally:
                await browser.close()
    except Exception as e:
        return {"status": "error", "error": f"Playwright falló: {e}"}


# ------------------------------------------------------------------------- #
# Login interactivo (UI) con sesión en memoria + 2FA
# ------------------------------------------------------------------------- #

class LoginSession:
    """Estado de un login en curso, indexado por session_id. Mantiene el
    navegador abierto mientras se espera el código 2FA del usuario."""

    def __init__(self, username: str, password: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.username = username
        self.password = password
        self.status = "logging_in"  # logging_in | needs_2fa | ok | error | expired
        self.error = ""
        self.twoFA_future: "asyncio.Future[str] | None" = None
        self.created = time.time()
        self.task: "asyncio.Task | None" = None


_SESSIONS: dict[str, LoginSession] = {}
_SESSIONS_LOCK = asyncio.Lock()


async def _sweep_sessions() -> None:
    """Expira sesiones que superaron SESSION_TTL (cierra su navegador al
    cancelar la tarea). Llamado perezosamente en cada operación de sesión."""
    now = time.time()
    stale = [sid for sid, s in _SESSIONS.items() if now - s.created > SESSION_TTL]
    for sid in stale:
        s = _SESSIONS.pop(sid, None)
        if s and s.task and not s.task.done():
            s.task.cancel()
        if s and s.twoFA_future and not s.twoFA_future.done():
            s.twoFA_future.cancel()


async def start_login(cookies_file: str, username: str | None = None,
                      password: str | None = None) -> dict[str, Any]:
    """
    Arranca un login interactivo. Creds: body (username/password) > env.
    Devuelve {"session_id":..., "status":"logging_in"} o {"status":"error",...}.
    El resultado real se consulta vía session_status(session_id) (polling).
    """
    await _sweep_sessions()
    if not playwright_available():
        return {"status": "error", "error": "Playwright no instalado."}
    u = (username or "").strip() or IG_USERNAME
    pw = (password or "").strip() or IG_PASSWORD
    if not u or not pw:
        return {"status": "error",
                "error": "Faltan credenciales: fija INSTAGRAM_USERNAME/PASSWORD "
                         "o envía username/password en la petición."}
    s = LoginSession(u, pw)
    s.twoFA_future = asyncio.get_running_loop().create_future()
    async with _SESSIONS_LOCK:
        _SESSIONS[s.id] = s
    s.task = asyncio.create_task(_login_task(s, cookies_file))
    return {"session_id": s.id, "status": s.status}


async def _login_task(s: LoginSession, cookies_file: str) -> None:
    from playwright.async_api import async_playwright
    try:
        async with async_playwright() as p:
            browser = await _launch(p)
            try:
                ctx = await _new_context(browser, STATE_FILE)
                # Reutilizar state/cookies existentes (mantener sesión si vive).
                if not state_exists() and cookies_file and os.path.isfile(cookies_file):
                    cks = cookies_to_playwright(cookies_file)
                    if cks:
                        await ctx.add_cookies(cks)
                page = await ctx.new_page()
                await page.goto("https://www.instagram.com/accounts/login/",
                                wait_until="domcontentloaded", timeout=NAV_TIMEOUT * 1000)
                await _fill_login(page, s.username, s.password)
                res = await _resolve_login(page, ctx, cookies_file,
                                           twoFA_future=s.twoFA_future, session=s)
                s.status = res["status"]
                s.error = res.get("error", "")
            finally:
                await browser.close()
    except asyncio.CancelledError:
        s.status = "expired"
        raise
    except Exception as e:
        s.status = "error"
        s.error = f"Playwright falló: {e}"


async def submit_2fa(session_id: str, code: str) -> dict[str, Any]:
    """Entrega el código 2FA del usuario a la sesión en espera."""
    await _sweep_sessions()
    s = _SESSIONS.get(session_id)
    if not s:
        return {"status": "error", "error": "Sesión no encontrada o expirada."}
    if not s.twoFA_future or s.twoFA_future.done():
        return {"status": "error", "error": "La sesión no está esperando un código 2FA."}
    s.twoFA_future.set_result(code.strip())
    return {"status": "ok"}


def session_status(session_id: str) -> dict[str, Any]:
    s = _SESSIONS.get(session_id)
    if not s:
        return {"status": "expired", "error": "Sesión no encontrada o expirada."}
    return {"status": s.status, "error": s.error}


# ------------------------------------------------------------------------- #
# Self-check (sin red, sin navegador): round-trip de conversión de cookies.
# ponytail: el truco no-trivial (Netscape<->Playwright) deja un check runnable.
# ------------------------------------------------------------------------- #

def _selfcheck() -> None:
    import tempfile
    net = (
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tABC123\n"
        "instagram.com\tFALSE\t/\tFALSE\t0\tcsrftoken\tDEF456\n"
        "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t1893456000\tdatr\tXYZ789\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(net)
        path = f.name
    try:
        pws = cookies_to_playwright(path)
        assert len(pws) == 3, f"esperaba 3 cookies, got {len(pws)}"
        sess = next(c for c in pws if c["name"] == "sessionid")
        assert sess["domain"] == ".instagram.com" and sess["secure"] is True \
            and sess["expires"] == 1893456000, sess
        csrf = next(c for c in pws if c["name"] == "csrftoken")
        assert csrf["domain"] == "instagram.com" and csrf["secure"] is False \
            and csrf["expires"] == -1, csrf  # exp 0 -> session (-1)
        datr = next(c for c in pws if c["name"] == "datr")
        assert datr["httpOnly"] is True and datr["domain"] == ".instagram.com", datr
        # Round-trip de vuelta a Netscape.
        out = tempfile.NamedTemporaryFile(suffix=".txt", delete=False).name
        try:
            write_netscape(pws, out)
            back = cookies_to_playwright(out)
            assert len(back) == 3, back
            sess2 = next(c for c in back if c["name"] == "sessionid")
            assert sess2["value"] == "ABC123" and sess2["expires"] == 1893456000, sess2
            # httpOnly se pierde al reescribir (Netscape básico no lo codifica sin
            # prefijo): aceptamos la pérdida, es esperada.
        finally:
            os.unlink(out)
        print(f"ig_auth self-check OK: {len(pws)} cookies, round-trip correcto.")
    finally:
        os.unlink(path)


if __name__ == "__main__":
    _selfcheck()