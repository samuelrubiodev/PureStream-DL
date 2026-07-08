# Imagen ligera: Python + binarios multimedia (gallery-dl, yt-dlp, ffmpeg)
FROM python:3.11-slim

# Pin de Tailwind CLI standalone (v3 = config clásico; evita rupturas de v4).
ENV TAILWIND_VERSION=3.4.17

# ffmpeg para merging de formatos de yt-dlp; ca-certificates para TLS; curl para Tailwind.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Tailwind CLI standalone (sin Node): compila un CSS de producción mínimo con
# solo las clases usadas en el HTML. Detecta arquitectura (x64 / arm64 homelab).
RUN ARCH=$(case "$(uname -m)" in x86_64|amd64) echo linux-x64;; aarch64|arm64) echo linux-arm64;; *) echo linux-x64;; esac) \
    && curl -fsSL -o /usr/local/bin/tailwindcss \
       "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-${ARCH}" \
    && chmod +x /usr/local/bin/tailwindcss

WORKDIR /app

# Dependencias de Python (FastAPI/uvicorn/httpx/multipart) + las herramientas CLI.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gallery-dl yt-dlp

# Código de la aplicación
COPY . .

# Genera iconos PNG 192/512 para la PWA (Python stdlib, sin dependencias).
RUN python3 static/gen_icons.py

# Compila el CSS de producción (sin CDN, sin warning de "not for production").
RUN tailwindcss -i src/input.css -o static/tailwind.css --minify

EXPOSE 8000

# Sonda de salud ligera (sin curl extra)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health').status==200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]