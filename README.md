<div align="center">

# Blur

### Detect. Review. Redact. Verify.

An enterprise style PII detection and redaction platform for **PDFs, DOCX files and scanned images**, with a hybrid rules + Transformer detection engine and a human-in-the-loop review workflow.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)
![TypeScript](https://img.shields.io/badge/TypeScript-6-3178C6?logo=typescript&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15%2B-4169E1?logo=postgresql&logoColor=white)
![PyTorch](https://img.shields.io/badge/PyTorch-CPU-EE4C2C?logo=pytorch&logoColor=white)
![Hugging Face](https://img.shields.io/badge/Transformers-HF-FFD21E?logo=huggingface&logoColor=black)

![Last commit](https://img.shields.io/github/last-commit/YOUR_USERNAME/Blur)
![Stars](https://img.shields.io/github/stars/YOUR_USERNAME/Blur?style=social)
![Issues](https://img.shields.io/github/issues/YOUR_USERNAME/Blur)
![Repo size](https://img.shields.io/github/repo-size/YOUR_USERNAME/Blur)

[Features](#-features) · [Architecture](#-architecture) · [Quick start](#-quick-start) · [ML engine](#-ml-engine) · [Roles](#-role-based-access-control) · [Testing](#-testing) · [Docs](#-documentation)

</div>

---

## Overview

Organisations handle documents full of personal data. Blur scans them, shows every detected item on a visual overlay, lets a human decide what to remove, then **physically removes** it from the file and checks that nothing leaked. It was built with Indian identifiers in mind (Aadhaar, PAN, voter id, IFSC, UPI) and handles general ones too.

> **Honest note:** this is a university project. It has not had an independent security assessment, and nothing here is a compliance claim for GDPR, DPDP Act or HIPAA. See [`docs/security.md`](docs/security.md).

## Features

| | Feature | Details |
|---|---|---|
| **Detect** | Dual-engine PII detection | 16 deterministic rules (Verhoeff for Aadhaar, format validators, context scoring) plus a fine-tuned multilingual Transformer, merged by a documented conflict policy |
| | OCR | ONNX-based RapidOCR for scanned PDFs and images, no Tesseract install |
| | 17 entity types | Person, Aadhaar, PAN, passport, voter id, driving licence, phone, email, address, date of birth, bank account, IFSC, UPI, vehicle reg, customer id, employee id, other identifiers |
| **Review** | Human-in-the-loop UI | Bounding-box overlays, bulk decisions by confidence, manual spans, reviewer assignment |
| **Redact** | Physical redaction | PDF text and pixels removed, DOCX runs edited, images filled, TXT masked; the original is never modified |
| **Verify** | Leak verification gate | Output is re-extracted and re-OCR'd; a failed check keeps no file |
| **Secure** | Accounts and audit | Argon2id, JWT with refresh rotation, lockout, RBAC, Fernet encryption at rest, SHA-256 hash-chained audit log |

## Architecture

```mermaid
flowchart TD
    U["User / Reviewer / Admin"] --> FE["React 19 + TypeScript SPA"]
    FE -->|"REST over HTTPS"| API["FastAPI backend"]
    API --> AUTH["Auth, RBAC, rate limits"]
    API --> ING["Upload validation"]
    ING --> EX["Text extraction (PyMuPDF, python-docx)"]
    EX -->|"scans and images"| OCR["RapidOCR (ONNX)"]
    EX --> DET
    OCR --> DET
    subgraph DET["Detection engine"]
        direction LR
        R["Rules + checksums"]
        T["Transformer NER (MiniLM, BIO)"]
        H["Hybrid resolver"]
        R --> H
        T --> H
    end
    DET --> DB[("PostgreSQL")]
    DB --> REV["Human review"]
    REV --> RED["Physical redaction"]
    RED --> VER["Leak verification"]
    VER --> FS[("Encrypted file store")]
    API --> AUD["SHA-256 audit ledger"]
    AUD --> DB
```

<details>
<summary><b>Request lifecycle (click to expand)</b></summary>

```mermaid
sequenceDiagram
    actor User
    participant API as FastAPI
    participant Det as Detection engine
    participant DB as PostgreSQL
    participant Red as Redaction and verification
    User->>API: Upload document
    API->>DB: Create document (validated, encrypted at rest)
    API->>Det: Background scan (extract, OCR if needed)
    Det->>DB: Store candidate entities
    User->>API: Review: redact or keep each entity
    API->>Red: Redact selected entities
    Red->>Red: Re-extract and re-OCR output
    Red-->>User: Verified redacted file
```

</details>

## Tech stack

| Layer | Technologies |
|---|---|
| **Backend** | Python 3.12, FastAPI, Uvicorn, Pydantic v2, SQLAlchemy 2, Alembic |
| **Database** | PostgreSQL 15+ |
| **Auth and security** | Argon2id, PyJWT, `cryptography` (Fernet) |
| **Documents** | PyMuPDF, python-docx, Pillow, RapidOCR (ONNX) |
| **Machine Learning** | PyTorch (CPU), Hugging Face `transformers`, `safetensors`, token-level BIO sequence labeling |
| **Rate limiting** | SlowAPI (in-memory or Redis-backed) |
| **Frontend** | React 19, TypeScript, Vite, Tailwind CSS v4, TanStack Query |
| **Testing** | pytest, Vitest, Playwright, axe |

## Quick start

**Prerequisites:** Python 3.12, Node.js 20+, PostgreSQL 15+. About 4 GB of free RAM if you run the Transformer.

<details open>
<summary><b>1. Database</b></summary>

```sql
CREATE ROLE pii_sentinel LOGIN PASSWORD 'change-me';
CREATE DATABASE pii_sentinel OWNER pii_sentinel;
```

</details>

<details open>
<summary><b>2. Backend</b></summary>

```bash
cd backend
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# With the ML model: install the CPU build of PyTorch first
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

alembic upgrade head                 # create tables
python -m scripts.seed               # development only: demo accounts
uvicorn app.main:app --reload        # http://localhost:8000
```

Create a `.env` in the project root (the backend reads `.env` from the root or `backend/`):

```ini
ENVIRONMENT=development
SECRET_KEY=change-me-in-env
DATABASE_URL=postgresql+psycopg://pii_sentinel:change-me@localhost:5432/pii_sentinel
CORS_ORIGINS=http://localhost:5173
NER_MODEL=                           # empty = rules-only mode; or a path to ml/artifacts/<run>/best
```

> Outside development the app refuses to start without a random `SECRET_KEY` (32+ characters) and Fernet `ENCRYPTION_KEYS`. Never run `scripts.seed` in production.

</details>

<details open>
<summary><b>3. Frontend</b></summary>

```bash
cd frontend
npm install
npm run dev                          # http://localhost:5173
```

</details>

<details>
<summary><b>4. First administrator (production)</b></summary>

```bash
ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='<10+ characters>' python -m scripts.create_admin
```

</details>

<details>
<summary><b>Rules-only mode (no model, no PyTorch)</b></summary>

Leave `NER_MODEL` empty. The app still works with the deterministic rules; recall is lower and the scan page shows `rules-only`.

</details>

## ML engine

The Transformer is a fine-tuned `Multilingual-MiniLM-L12-H384` doing token classification over 35 BIO labels (17 entity types), trained on CPU in about 19 minutes. Detections from rules and the model are merged by `backend/app/ml/hybrid.py`, and every original prediction is kept for audit.

**Held-out results** (strict = exact start, end and type):

| Test set | Rules | Transformer | **Hybrid** |
|---|---|---|---|
| Ylemis (10k docs, 14 slices) | 60.3% | 68.6% | **74.3%** |
| Maskara template-disjoint (15k) | 41.4% | 80.0% | **80.6%** |
| Maskara real-world (2.6k) | 51.4% | 85.9% | **86.0%** |

<details>
<summary><b>Reproduce training and evaluation</b></summary>

Run from `backend/` with the virtual environment active:

```bash
python ../ml/scripts/download_data.py
python ../ml/scripts/prepare.py
python ../ml/scripts/train.py --config ../ml/configs/train_minilm_quick.yaml
python ../ml/scripts/evaluate.py --model ../ml/artifacts/pii-minilm-quick/best --name my-run
```

Then set `NER_MODEL=<path to .../best>` in `.env`. The checkpoint (about 470 MB) is not in Git.

</details>

<details>
<summary><b>Limitations (read before trusting the numbers)</b></summary>

- Trained and tested on **synthetic** data only; expect lower scores on real documents.
- 53.9% of PII-free Ylemis documents get at least one false prediction.
- Weak types: `OTHER_IDENTIFIER`, `VEHICLE_REG`. Weaker languages: Hindi, OCR-style text.
- Only English and Hinglish were in the training data; other Indian languages are not claimed as supported.
- Confidence scores are not calibrated probabilities.

Full write-up: [`docs/ml-pipeline.md`](docs/ml-pipeline.md).

</details>

## Role-based access control

| Role | Can do |
|---|---|
| **USER** | Register (if enabled), upload, scan, download and delete own documents, assign reviewers |
| **REVIEWER** | Work the assigned-documents queue, decide on PII entities, download redacted files. Cannot access the original files of assigned documents |
| **ADMIN** | Manage users and roles, unlock accounts, read the audit log |

## Project structure

```
Blur/
├── backend/        FastAPI app, Alembic migrations, scripts, pytest suite
│   └── app/        api/ ml/ document_processing/ redaction/ security/ services/
├── frontend/       React + TypeScript SPA, Vitest and Playwright tests
├── ml/             data prep, training, evaluation, configs, results
├── docs/           HLD, ML pipeline, security, deployment, testing, user guide
└── tests/fixtures/ fictional sample documents
```

## Testing

| Suite | Command | Last recorded result |
|---|---|---|
| Backend | `cd backend && python -m pytest` | 366 passed |
| Frontend unit | `cd frontend && npm test` | 19 passed |
| Browser end-to-end | `cd frontend && npx playwright test` | 12 passed |

Backend tests need `TEST_DATABASE_URL` pointing to a database whose name ends in `_test`.

## Documentation

| Document | Contents |
|---|---|
| [`docs/HLD.md`](docs/HLD.md) | High-level design and diagrams |
| [`docs/ml-pipeline.md`](docs/ml-pipeline.md) | Data, training, evaluation, known weaknesses |
| [`docs/security.md`](docs/security.md) | Controls and residual risks |
| [`docs/deployment.md`](docs/deployment.md) | Running on a server |
| [`docs/testing.md`](docs/testing.md) | What is tested and how |
| [`docs/user-guide.md`](docs/user-guide.md) | Using the application |
| [`PROJECT_WORKFLOW_AND_OVERVIEW.md`](PROJECT_WORKFLOW_AND_OVERVIEW.md) | Full workflow and API reference |

## Known gaps

No MFA, no email verification or self-service password reset, rate limits are per process unless Redis is configured, and parsing untrusted files runs inside the web process. Details and mitigations are in [`docs/security.md`](docs/security.md).

## Contributing

1. Fork the repo and create a branch: `git checkout -b feature/your-change`
2. Use fictional data only; never commit real personal data.
3. Run the backend and frontend tests before opening a pull request.

## Team

Built at the School of Computer Engineering, KIIT, Bhubaneswar. Add the team and roles here.

<div align="center">

If Blur helped you, consider giving it a star.

</div>
