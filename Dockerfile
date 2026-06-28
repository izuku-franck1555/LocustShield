FROM python:3.12-slim

WORKDIR /app

# geopandas / shapely / pyproj / pyogrio ship manylinux wheels that bundle
# GEOS / PROJ / GDAL, so no system packages are needed on python:3.12-slim.
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
