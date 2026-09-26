# syntax=docker/dockerfile:1.7
#
# Shared image for the API and the worker. Media binaries are installed from
# the distribution so their versions are identifiable in job diagnostics
# (spec 7.1), and the container runs as a non-root user with no extra
# capabilities (spec 7.2).

FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg/ffprobe for all media work; libgl1 + libglib2.0-0 are what the
# opencv-python wheel that PySceneDetect depends on needs to import.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      libgl1 \
      libglib2.0-0 \
      tini \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements.txt is the compiled lock: every transitive dependency is pinned
# to an exact version, so the build is reproducible.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# A fixed uid keeps the shared /data volume's ownership stable across rebuilds.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /data /app

USER appuser

ENV PYTHONPATH=/app \
    DATA_DIR=/data

# tini reaps the FFmpeg subprocesses a terminated job leaves behind.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]
CMD ["api"]
