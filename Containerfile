# The pmtiles CLI builds the map basemap extract (see fetch_basemap.sh). Taken
# from a build stage so the release image gets the binary without the tarball.
FROM debian:bookworm-slim AS pmtiles
ARG PMTILES_VERSION=1.31.1
ADD https://github.com/protomaps/go-pmtiles/releases/download/v${PMTILES_VERSION}/go-pmtiles_${PMTILES_VERSION}_Linux_x86_64.tar.gz /tmp/pmtiles.tar.gz
RUN tar xzf /tmp/pmtiles.tar.gz -C /usr/local/bin pmtiles && chmod 0755 /usr/local/bin/pmtiles

FROM python:3.12-slim

# The GTFS database lives on a volume at /data, not in the image: it is a
# build artifact, it is large once built from the real feed, and it is
# refreshed weekly by re-running ingest_gtfs.py against the same volume.
ENV GTFS_DB=/data/gtfs.sqlite3 \
    BASEMAP_DIR=/data/basemap \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is used by fetch_basemap.sh to find the newest Protomaps build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=pmtiles /usr/local/bin/pmtiles /usr/local/bin/pmtiles

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --chmod so the image does not inherit restrictive host file modes.
COPY --chmod=0644 app.py ingest_gtfs.py ./
COPY --chmod=0755 fetch_basemap.sh ./
# NOT --chmod=0644 here: that applies to directories as well, and a directory
# without its execute bit cannot be traversed. Starlette turns the resulting
# PermissionError into a 401, so every file under static/vendor and static/fonts
# would 401. `a+rX` grants read to all and execute to directories only.
COPY static/ ./static/
RUN chmod -R a+rX ./static

# Run unprivileged. /data is created and owned here so that a named podman
# volume mounted over it inherits this ownership on first use.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data
USER appuser
VOLUME /data

EXPOSE 8000

# Default is the server; the ingest is the same image with an argv override:
#   podman run --rm -v translink-data:/data translink-departures \
#       python ingest_gtfs.py
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
