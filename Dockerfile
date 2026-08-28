FROM python:3.11-slim-bookworm

ARG DENO_VERSION=2.3.7
ARG BGUTIL_VERSION=1.3.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DENO_NO_UPDATE_CHECK=1 \
    DENO_NO_PROMPT=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        git \
        unzip \
    && curl -fsSL \
        "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
        -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /usr/local/bin \
    && chmod 0755 /usr/local/bin/deno \
    && rm -f /tmp/deno.zip \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY migrations ./migrations

RUN python -m pip install --no-cache-dir ".[cloud]"

RUN useradd --create-home --uid 10001 audioarchive \
    && mkdir -p /work/jobs /app/tools \
    && chown -R audioarchive:audioarchive /work /home/audioarchive /app/tools

USER audioarchive

RUN git clone --depth 1 --single-branch --branch "${BGUTIL_VERSION}" \
       https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
       /app/tools/bgutil-ytdlp-pot-provider \
    && cd /app/tools/bgutil-ytdlp-pot-provider/server \
    && deno install --allow-scripts=npm:canvas --frozen \
    && test -f /app/tools/bgutil-ytdlp-pot-provider/server/src/main.ts

CMD ["python", "-m", "audio_archive.cloud.runtime", "web"]
