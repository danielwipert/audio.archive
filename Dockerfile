FROM brainicism/bgutil-ytdlp-pot-provider:1.3.1-deno AS bgutil

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AUDIO_ARCHIVE_SCRATCH_ROOT=/work/jobs

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=bgutil /usr/bin/deno /usr/local/bin/deno
COPY --from=bgutil /app /app/tools/bgutil-ytdlp-pot-provider/server

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY migrations ./migrations

RUN python -m pip install --no-cache-dir ".[cloud]" \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin audioarchive \
    && mkdir -p /work/jobs \
    && chown -R audioarchive:audioarchive /work /app

USER audioarchive

EXPOSE 8000

CMD ["audio-archive-cloud-web"]
