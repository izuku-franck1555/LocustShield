# Deploying LocustShield

LocustShield is a single FastAPI process that serves **both** the frontend (`/`)
and the API (`/api`) on one port. There is no separate database or build step.

> The first request for a given region/month fetches gridded climate from NASA
> POWER (a few seconds) and then caches it on disk, so later requests are fast.
> Keep the climate cache on a persistent volume (Docker) or a writable path
> (bare metal) so it survives restarts.

The repository is **private**. On the VPS you must authenticate before cloning
(a GitHub token, a deploy key, or `gh auth login`).

---

## Option A — Docker (recommended)

Requires Docker Engine + the Compose plugin.

```bash
# 1. Get the code (private repo — use a token or deploy key)
git clone https://github.com/izuku-franck1555/LocustShield.git
cd LocustShield

# 2. Build and start (detached)
docker compose up -d --build

# 3. Check
docker compose logs -f          # follow logs
curl http://localhost:8000/api/regions
```

The app is now on `:8000`. The Compose file maps `8000:8000`, restarts the
container `unless-stopped`, and keeps the climate cache in the named volume
`locustshield-cache` (mounted at `/app/data/cache`).

Update to a new version:

```bash
git pull
docker compose up -d --build
```

### Nginx reverse proxy (TLS-ready)

Put Nginx in front so you can serve a domain and add HTTPS:

```nginx
server {
    listen 80;
    server_name locustshield.example.org;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;   # first per-region NASA POWER fetch can take a few seconds
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/locustshield /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# Optional TLS via Let's Encrypt
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d locustshield.example.org
```

---

## Option B — Bare metal (venv + systemd)

Requires Python 3.12.

```bash
git clone https://github.com/izuku-franck1555/LocustShield.git
cd LocustShield

python3.12 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip wheel
./.venv/bin/python -m pip install -r requirements.txt gunicorn

# quick test
./.venv/bin/uvicorn backend.api:app --host 0.0.0.0 --port 8000
```

For production, run Uvicorn workers under Gunicorn, supervised by systemd.
Create `/etc/systemd/system/locustshield.service`:

```ini
[Unit]
Description=LocustShield (R0 suitability web-GIS)
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/LocustShield
ExecStart=/opt/LocustShield/.venv/bin/gunicorn backend.api:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers 2 --bind 127.0.0.1:8000 --timeout 120
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now locustshield
sudo systemctl status locustshield
```

Then front it with the same Nginx block as Option A.

---

## Notes

- **Ports:** the app listens on `8000` and serves the SPA at `/` and the API at
  `/api`. Only expose `8000` (directly or, preferably, via Nginx).
- **Climate cache:** `data/cache/` is created on demand and is safe to delete
  (it just re-fetches from NASA POWER). Persist it for speed.
- **Boundaries:** country boundaries ship in `data/boundaries/` (local store),
  so the map works without contacting a boundary server at runtime.
- **No secrets:** NASA POWER and geoBoundaries are keyless; there is no `.env`
  and nothing to configure to run.
