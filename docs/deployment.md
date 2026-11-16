# Deployment guide

How to run PII Sentinel on a server you control. Read `docs/security.md` first: this app handles sensitive documents and
has known gaps (no application-level encryption at rest, open registration, no MFA).

**What was actually tested.** The application, migrations, tests and browser tests were run on Windows 11 with PostgreSQL 17,
Python 3.12 and Node 24. Everything below about Linux, systemd, nginx and Docker is standard practice but was **not run
here**; check each step in a staging environment first. No Dockerfiles are provided.

## 1. Architecture

```
Browser --HTTPS--> reverse proxy (nginx/Caddy) --+--> static files: frontend/dist
                                                  +--> /api/ and /health/ --> uvicorn (FastAPI, 127.0.0.1:8000, ONE worker)
                                                                               |-- PostgreSQL 17
                                                                               |-- storage directory (uploaded + redacted files)
                                                                               |-- OCR models (inside the pip package)
                                                                               `-- optional fine-tuned PII model (folder on disk)
```

* The web app is a static single-page app. Serve it and the API from the **same origin** (same host name and scheme). The
  refresh cookie is scoped to `/api/v1/auth`, and the API checks the browser `Origin` header against `CORS_ORIGINS`.
* Scans run inside the API process (background tasks). Redaction runs while the request waits.

## 2. Requirements

| | Minimum | Notes |
|---|---|---|
| OS | Windows or Linux x86-64 | Developed on Windows |
| Python | 3.12 | other versions untested |
| Node.js | 20+ (built with 24) | needed only to build the frontend, not to run it |
| PostgreSQL | 15+ (developed on 17) | |
| RAM | 4 GB free (with the model) | The PII model plus PyTorch use about 1.1 GB; OCR and PDF work add more. Rules-only mode needs about 1 GB less |
| Disk | 3 GB + documents | PyTorch CPU ~0.3 GB, model 0.47 GB, OCR models, plus uploads |
| CPU | 2+ cores | No GPU needed. A scan of a short document takes under a second; first scan after a restart waits for model loading (10-30 s) |
| Network | none required at run time | Install-time downloads only (pip, npm, PyTorch wheels) |

## 3. Get the code and the model

```bash
git clone <your repository> pii-sentinel && cd pii-sentinel
```

The fine-tuned model is **not in Git** (`ml/artifacts/` is ignored). Copy this folder from the machine that trained it
(it is self-contained: weights, tokenizer, config, metadata, about 465 MB):

```
ml/artifacts/pii-minilm-quick/best/   ->   /opt/pii-sentinel/models/pii-minilm/
```

Or retrain it (about 20 minutes on a laptop CPU; see `ml/README.md`). Without a model the app still works in
**rules-only mode** (lower recall; the scan page shows `rules-only`). Leave `NER_MODEL` empty and skip PyTorch.

## 4. Database

```sql
CREATE ROLE pii_sentinel LOGIN PASSWORD '<strong password>';
CREATE DATABASE pii_sentinel OWNER pii_sentinel;
```

Allow the app host in `pg_hba.conf` only, and use TLS (`?sslmode=require` in the URL) if the database is on another machine.

## 5. Backend

```bash
cd backend
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate

# (a) with the PII model: install PyTorch (CPU build) first, then everything else
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
# (b) rules-only: skip torch, and remove the "ML" block (torch, transformers, ...) from requirements.txt before installing
```

`requirements.lock.txt` holds the exact versions that were tested (`pip install -r requirements.lock.txt` after installing
torch, if you want to reproduce them). `requirements.txt` also lists packages used only for training and for dependency
auditing (`pyarrow`, `pandas`, `pyyaml`, `psutil`, `pip-audit`); they are not needed to serve requests.

### Configuration

Copy `.env.example` to `.env` in the **project root** (or set real environment variables). Minimum for production:

```ini
ENVIRONMENT=production
SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(48))">
DATABASE_URL=postgresql+psycopg://pii_sentinel:<password>@localhost:5432/pii_sentinel
CORS_ORIGINS=https://pii.example.com
STORAGE_DIR=/var/lib/pii-sentinel/storage
NER_MODEL=/opt/pii-sentinel/models/pii-minilm
NER_LOCAL_ONLY=true
```

The full list of settings, with comments, is in `.env.example`. Important behaviour:

* With `ENVIRONMENT` other than `development`, the app **refuses to start** if `SECRET_KEY` is missing, the placeholder, or
  shorter than 32 characters. It also hides `/docs` and `/openapi.json`, adds CSP and HSTS headers, and marks the refresh
  cookie `Secure` (so the site must be served over HTTPS or login will not persist).
* `CORS_ORIGINS` must contain the exact public origin (scheme + host [+ port], no trailing slash). If it does not match,
  the browser's refresh and logout calls get **403 untrusted origin** and users are signed out after 15 minutes.
* **Changing `SECRET_KEY` invalidates every access token** (users must sign in again).
* Create the storage directory, owned by the app user, mode 700, **outside the web root**.

### Initialise the database

```bash
cd backend
alembic upgrade head                 # creates all tables; safe to re-run
```

Create the first administrator (there is no sign-up path to ADMIN). The password comes from the environment so it stays
out of shell history:

```bash
ADMIN_EMAIL=you@yourdomain.com ADMIN_PASSWORD='<12+ char password>' ADMIN_NAME='Your Name' python -m scripts.create_admin
```

Do **not** run `python -m scripts.seed` in production: it creates well-known demo accounts (`admin@example.com`, ...).
Running `create_admin` again for an existing email resets its password and re-enables it.

### Run

```bash
cd backend
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips="127.0.0.1"
```

* **Use exactly one worker.** At start-up the app marks scans left running by a previous process as failed, and the rate
  limiter and model cache live in memory. Several workers would fail each other's scans.
* `--proxy-headers` plus `--forwarded-allow-ips` make the rate limits use the real client address from the proxy instead
  of the proxy's own. Set the allowed IP to your proxy's address.
* Check: `curl http://127.0.0.1:8000/health/ready` returns `{"status":"ready"}` once the database answers.
* The first scan after a start may take 10-30 seconds while the OCR and language models load (they load in the background
  at start-up, so waiting a minute before the first use avoids it).

#### systemd example (Linux, untested here)

```ini
# /etc/systemd/system/pii-sentinel.service
[Unit]
Description=PII Sentinel API
After=network.target postgresql.service

[Service]
User=piisentinel
WorkingDirectory=/opt/pii-sentinel/backend
EnvironmentFile=/opt/pii-sentinel/.env
ExecStart=/opt/pii-sentinel/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/pii-sentinel
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

On Windows run the same command under a service wrapper (for example NSSM) or Task Scheduler, with the environment set.

## 6. Frontend

```bash
cd frontend
npm ci
npm run build                        # writes frontend/dist (static files)
```

The build contains no secrets and no server address: it calls `/api/...` on whatever host serves it. Copy `frontend/dist`
to the proxy's web root.

## 7. Reverse proxy and HTTPS (nginx example, untested here)

```nginx
server {
    listen 443 ssl http2;
    server_name pii.example.com;
    ssl_certificate     /etc/letsencrypt/live/pii.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pii.example.com/privkey.pem;

    client_max_body_size 25m;                 # >= MAX_UPLOAD_MB + 1 MB (the API rejects larger bodies itself)

    root /var/www/pii-sentinel;               # contents of frontend/dist
    index index.html;
    location / { try_files $uri /index.html; }            # single-page-app routes

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 180s;               # redaction runs inside the request (limit: SCAN_TIMEOUT_SECONDS)
        proxy_request_buffering on;
    }
    location /health/ { proxy_pass http://127.0.0.1:8000; allow 127.0.0.1; deny all; }   # keep health checks internal

    # optional: close self-registration once your accounts exist (see "Registration" below)
    # location = /api/v1/auth/register { return 403; }

    add_header X-Content-Type-Options nosniff always;
}
server { listen 80; server_name pii.example.com; return 301 https://$host$request_uri; }
```

Caddy does the same with `reverse_proxy /api/* 127.0.0.1:8000`, `file_server`, `try_files {path} /index.html`, and gets
certificates automatically. Never expose uvicorn directly to the internet.

### Registration
Anyone who can reach the site can create a `USER` account. There is no setting to switch registration off and no admin
"create user" screen. Options: keep the app on a private network or VPN, protect the whole site with proxy authentication,
or block `POST /api/v1/auth/register` at the proxy (as in the commented line above) after your users have registered.
Admins can promote a user to `REVIEWER` or `ADMIN` and disable accounts on the Admin page.

## 8. Go-live checklist

- [ ] HTTPS works; HTTP redirects; the login cookie is `Secure` (browser dev tools, Application tab)
- [ ] `ENVIRONMENT=production`, random `SECRET_KEY`, `/docs` returns 404
- [ ] `CORS_ORIGINS` equals the public origin; staying logged in past 15 minutes works
- [ ] Database not reachable from the internet; app database user is not a superuser
- [ ] `STORAGE_DIR` is private, outside the web root, on an encrypted disk if documents are sensitive
- [ ] Demo accounts absent (`SELECT email FROM users;`); a strong admin password set
- [ ] Upload a fictional test document end to end: scan, review, redact, download, and check the file
- [ ] The scan shows `hybrid` (model loaded), not `rules-only`, if you installed the model
- [ ] Backups configured **and a restore tested** (below)
- [ ] You have read the residual risks in `docs/security.md`

## 9. Operations

**Backups.** Back up the database and `STORAGE_DIR` together: the database references files by key, and the extracted text
and detected values live only in the database.
```bash
pg_dump -Fc -U pii_sentinel pii_sentinel > pii_$(date +%F).dump
rsync -a /var/lib/pii-sentinel/storage/ /backups/pii-storage/
```
Backups contain sensitive data: encrypt them and restrict access. Restore: create an empty database, `pg_restore -d pii_sentinel
file.dump`, restore the storage folder, start the app.

**Upgrading.**
```bash
git pull
cd backend && pip install -r requirements.txt && alembic upgrade head
cd ../frontend && npm ci && npm run build        # then copy dist to the web root
sudo systemctl restart pii-sentinel
```
Take a database backup first. Migrations are tested forwards and backwards (`alembic downgrade -1`), but downgrading after
new data exists can drop that data.

**Logs.** The API logs to stdout (journald under systemd). Application logs and the audit log never contain detected values,
passwords or tokens. Every response carries `X-Request-ID`; the audit log stores it. Admins read audit events on the Admin page.

**Monitoring.** Poll `/health/live` (process up) and `/health/ready` (database reachable). A scan stuck in `PROCESSING`
longer than `SCAN_TIMEOUT_SECONDS` + 60 s is failed automatically on the next scan request or restart.

**Cleaning up.** Deleting a document in the app removes its files, scans, detections and redacted outputs. Audit rows stay.
There is no automatic retention; delete old documents with the app (or the API) on your own schedule.

**Changing the model.** Point `NER_MODEL` at another checkpoint folder and restart. Each scan records which model version
produced it.

## 10. Docker (outline only, untested)

No Dockerfiles ship with the project. If you containerise it: one image for the API (Python 3.12 slim, install PyTorch
from the CPU index first, then `requirements.txt`; the OCR package pulls in OpenCV, which usually needs the system packages
`libgl1` and `libglib2.0-0`), one nginx image serving `frontend/dist`, and PostgreSQL. Mount the model folder read-only, mount
`STORAGE_DIR` as a persistent volume, pass settings as environment variables, run `alembic upgrade head` before starting the
API, and keep the API at one replica with one worker.

## 11. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| App exits at start with `SECRET_KEY must be a random value...` | Production mode with a weak key. Set a 32+ character random `SECRET_KEY` |
| Users are signed out every 15 minutes; browser shows 403 on `/auth/refresh` | `CORS_ORIGINS` does not equal the site's origin (scheme, host, port) |
| Login works but session is lost on reload | Cookie is `Secure` but the site is served over plain HTTP; use HTTPS. Or the proxy is not forwarding `/api/` |
| 413 on upload | `client_max_body_size` (proxy) or `MAX_UPLOAD_MB` too small |
| 502 / 504 on redact of a big file | Raise `proxy_read_timeout`; large scanned PDFs are slow |
| Scans always show `rules-only`; scan details show `ner_error` | Model path wrong or PyTorch/transformers not installed; the server log shows the load error. Fix the path, restart (a failed load is retried every 60 s) |
| First scan is very slow, later ones fast | Models load on first use; keep `OCR_WARMUP=true` and wait a minute after a restart |
| Scanned PDFs/images fail with "OCR engine is not available" | `rapidocr-onnxruntime` or its OpenCV/onnxruntime dependencies are missing (on Linux install `libgl1`) |
| `alembic` cannot connect | `DATABASE_URL` wrong, or run it from `backend/` with the same environment as the app |
| Scans stuck on "Scan running..." | Server restarted mid-scan: it is marked failed at start-up; press Retry. If it keeps happening check memory (OOM kills) |
| Too many requests (429) | Auth limit is 10/minute and uploads/scans/redactions 60/minute per client address; behind a proxy make sure `--proxy-headers` is set, otherwise everyone shares one address |
| `/docs` is 404 | Intended in production. Set `ENVIRONMENT=development` on a private machine to see the API docs |
