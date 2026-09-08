FROM python:3.12-slim

WORKDIR /app

# geopandas / shapely / pyproj / pyogrio / rasterio ship manylinux wheels that
# bundle GEOS / PROJ / GDAL (rasterio's GDAL includes curl, which the CHELSA
# provider needs for /vsicurl/ range reads), so no GDAL/GEOS system packages
# are needed on python:3.12-slim.
#
# ONE exception: rasterio's bundled GDAL links against libexpat, which the
# slim image does not carry. Without it `import rasterio` fails with
# "libexpat.so.1: cannot open shared object file" and every CHELSA request
# returns 502. libexpat1 is ~100 kB.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# If a future build cannot find a wheel for your platform, install the minimal
# native libs instead, e.g.:
#   RUN apt-get update && apt-get install -y --no-install-recommends \
#         libgdal-dev libgeos-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app (the host .venv / caches are excluded via .dockerignore).
COPY . .

EXPOSE 8000

CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8000"]
