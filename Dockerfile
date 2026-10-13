# Sikkim Landslide Platform — API + dashboard in one image.
#
# The image ships data/processed/ (~72 MB of COGs, GeoJSON and statistics), which
# is committed to the repository. The 881 MB raw delivery is NOT part of the build
# context — run `make data` locally to regenerate data/processed/ before building.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # GDAL re-reads the same COG blocks constantly; a real block cache is the
    # single biggest tile-latency win in the container.
    GDAL_CACHEMAX=256 \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    VSI_CACHE=TRUE \
    VSI_CACHE_SIZE=5000000 \
    PORT=8080

WORKDIR /app

# rasterio's wheel bundles GDAL but still links against the system expat.
# python:*-slim does not ship it, and without this the import fails at boot with
# "libexpat.so.1: cannot open shared object file".
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config/    config/
COPY backend/   backend/
COPY frontend/  frontend/
COPY pipeline/  pipeline/
COPY reference/ reference/
COPY data/processed/ data/processed/

# Fail at build time rather than at first request if the data was never built.
RUN test -f data/processed/stats.json \
    || (echo "ERROR: data/processed/stats.json missing — run 'make data' first." && exit 1)

RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/healthz').status==200 else 1)"

# Cloud Run injects $PORT. One worker on purpose: the feature store is held in
# memory and more workers would just multiply it.
CMD exec uvicorn backend.app:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 65
