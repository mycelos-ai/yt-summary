# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    YTS_DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno as a JavaScript runtime for yt-dlp's n-parameter challenge
# solver (EJS). Without it, yt-dlp can't decode YouTube's signed stream
# URLs and only storyboard images come back as "available formats".
# See https://github.com/yt-dlp/yt-dlp/wiki/EJS.
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) DENO_ARCH=x86_64-unknown-linux-gnu ;; \
        arm64) DENO_ARCH=aarch64-unknown-linux-gnu ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}.zip" -o /tmp/deno.zip; \
    unzip -q /tmp/deno.zip -d /usr/local/bin; \
    chmod +x /usr/local/bin/deno; \
    rm /tmp/deno.zip; \
    deno --version

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY app ./app

RUN mkdir -p /data

EXPOSE 8000

VOLUME ["/data"]

# --proxy-headers + --forwarded-allow-ips=* let uvicorn trust the
# X-Forwarded-Proto / X-Forwarded-Host headers set by a reverse proxy
# (Caddy, nginx, Traefik, …). Without these, request.url.scheme is
# always "http" inside the app and absolute URLs we render in HTML
# come out as http://… — which the browser blocks as Mixed Content
# when the user reaches us over HTTPS, breaking assets and HTMX swaps.
# The container is meant to sit behind a trusted proxy, so trusting
# all upstream IPs is the right default here.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
