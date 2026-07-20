FROM python:3.12-slim

# The GTFS database lives on a volume at /data, not in the image: it is a
# build artifact, it is large once built from the real feed, and it is
# refreshed weekly by re-running ingest_gtfs.py against the same volume.
ENV GTFS_DB=/data/gtfs.sqlite3 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --chmod so the image does not inherit restrictive host file modes.
COPY --chmod=0644 app.py ingest_gtfs.py ./
COPY --chmod=0644 static/ ./static/

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
