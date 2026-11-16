# PII Sentinel — Complete System Architecture & Workflow Guide

> **PII Sentinel** is an enterprise-grade, privacy-first Personally Identifiable Information (PII) detection, review, and redaction platform. It features specialized models and rule engines tuned for Indian and global identifiers, a multi-format physical redaction engine, human-in-the-loop review queues, dual-level encryption at rest, and cryptographic audit logging.

---

## Table of Contents
1. [Executive Summary & What the Project Does](#1-executive-summary--what-the-project-does)
2. [Technology Stack](#2-technology-stack)
3. [End-to-End System Workflow](#3-end-to-end-system-workflow)
   - [Phase 1: Authentication & Access Control](#phase-1-authentication--access-control)
   - [Phase 2: File Upload & Ingestion](#phase-2-file-upload--ingestion)
   - [Phase 3: Text & Layout Extraction](#phase-3-text--layout-extraction)
   - [Phase 4: Dual-Engine PII Detection & Hybrid Merging](#phase-4-dual-engine-pii-detection--hybrid-merging)
   - [Phase 5: Human-in-the-Loop Review & Delegation](#phase-5-human-in-the-loop-review--delegation)
   - [Phase 6: Multi-Format Physical Redaction](#phase-6-multi-format-physical-redaction)
   - [Phase 7: Zero-Trust Leak Verification Gate](#phase-7-zero-trust-leak-verification-gate)
   - [Phase 8: Tamper-Evident Cryptographic Audit Logging](#phase-8-tamper-evident-cryptographic-audit-logging)
   - [Phase 9: Background Scans & Data Retention](#phase-9-background-scans--data-retention)
4. [Role-Based Access Control (RBAC)](#4-role-based-access-control-rbac)
5. [Database Architecture & Data Models](#5-database-architecture--data-models)
6. [Security & Cryptography Model](#6-security--cryptography-model)
7. [API Route Reference](#7-api-route-reference)
8. [Configuration & Environment Variables](#8-configuration--environment-variables)
9. [Local Development & Deployment Guide](#9-local-development--deployment-guide)

---

## 1. Executive Summary & What the Project Does

Organizations process sensitive documents (PDFs, Word documents, images, scans) containing confidential personal data. Leaking this data violates regulatory frameworks like **DPDP Act (India)**, **GDPR (EU)**, and **HIPAA (US)**.

PII Sentinel solves this through a zero-trust pipeline:
- **Detects 17+ PII categories**, combining deterministic checksum rules (e.g. Verhoeff algorithm for Aadhaar, income tax structure for PAN) with multilingual Transformer NER models (XLM-RoBERTa / MiniLM).
- **Normalizes text into a Canonical Coordinate Space**, decoupling layout extraction from detection logic.
- **Enables Human Reviewers** to inspect suggestions, adjust confidence thresholds in bulk, reclassify entities, or highlight missed spans.
- **Physically Redacts Documents** by scrubbing PDF text and underlying image pixels, modifying DOCX XML runs without breaking styling, or blacking out images.
- **Verifies Redaction Completeness** by re-extracting and re-OCRing output files to ensure zero PII needles escape before writing to disk.
- **Signs an Append-Only SHA-256 Audit Ledger** using PostgreSQL advisory transaction locks to guarantee monotonic integrity.

---

## 2. Technology Stack

| Layer | Technologies |
| :--- | :--- |
| **Backend API** | Python 3.11 / 3.12, FastAPI 0.115, Uvicorn, Pydantic v2, Pydantic Settings |
| **Database & ORM** | PostgreSQL 15+ (tested on Neon Serverless Postgres), SQLAlchemy 2.0, Alembic migrations |
| **Authentication** | Argon2id password hashing, PyJWT (HS256 access tokens), HttpOnly secure refresh cookies |
| **Security & Cryptography** | Cryptography library (`MultiFernet` AES-128-CBC + HMAC-SHA256), SHA-256 hash chains |
| **Document Processing** | PyMuPDF (`fitz`), `python-docx`, Pillow (`PIL`), `charset-normalizer` |
| **OCR Engine** | RapidOCR ONNX runtime (embedded ONNX models, zero native Tesseract system dependencies) |
| **Machine Learning** | PyTorch (CPU), Hugging Face `transformers`, `safetensors`, token-level BIO sequence labeling |
| **Rate Limiting** | SlowAPI (in-memory or Redis-backed) |
| **Frontend SPA** | React 19, TypeScript, Vite, Tailwind CSS v4, `@tanstack/react-query`, Lucide React |

---

## 3. End-to-End System Workflow

```
   ┌────────────────────────────────────────────────────────────────────────┐
   │                           1. CLIENT BROWSER                            │
   │           Upload File ────► Review Entities ────► Trigger Redaction    │
   └───────────────┬───────────────────▲─────────────────────▲──────────────┘
                   │                   │                     │
═══════════════════╪═══════════════════╪═════════════════════╪═══════════════
                   │                   │                     │
┌──────────────────▼───────────────────┴─────────────────────┴──────────────┐
│                            2. FASTAPI BACKEND                             │
│                                                                           │
│  [Upload Ingestion]                                                       │
│    • Validate MIME & Magic Bytes (PDF, DOCX, PNG, JPG, TXT)               │
│    • Stream limit check (max 20MB) & zip-bomb protection                  │
│    • AES-128-CBC Encrypted write to storage/originals/{uuid}.ext          │
│    • Register Document row in PostgreSQL                                  │
│                                                                           │
│  [Async Scan Runner (Background Task)]                                    │
│    • Extract layout & word bboxes (PyMuPDF / docx / RapidOCR)            │
│    • Map coordinates into canonical 1D text character offsets             │
│    • Run Rules Engine (Aadhaar Verhoeff, PAN, Phone, DOB, IFSC, etc.)     │
│    • Run Transformer NER Engine (Sliding window token classification)     │
│    • Hybrid Merging: Union same-type overlaps; resolve conflicts          │
│    • Persist Scan & PiiEntity rows (EncryptedText in DB)                  │
│                                                                           │
│  [Human Review & Delegation Queue]                                        │
│    • Owner delegates document to REVIEWER-role accounts                   │
│    • Reviewers inspect masked spans on interactive visual text view       │
│    • Mark REDACT, KEEP, or FALSE_POSITIVE (Single or Bulk action)         │
│    • Adjust confidence thresholds (with re-apply override support)        │
│    • Manually add undetected PII spans                                    │
│                                                                           │
│  [Redaction Engine & Verification Gate]                                   │
│    • Gather all approved REDACT entity spans                              │
│    • PDF: fitz.apply_redactions(PDF_REDACT_IMAGE_PIXELS) + metadata wipe │
│    • DOCX: XML <w:t> run-level slicing, font & style preservation        │
│    • TXT / Images: offset string masking & black rectangle drawing       │
│    • ZERO-TRUST CHECK: Re-extract/OCR output file to verify PII absence   │
│    • Store redacted output in storage/redacted/{uuid}.ext                 │
│                                                                           │
│  [Tamper-Evident Audit Ledger]                                            │
│    • Compute SHA-256 hash: prev_hash | row_id | action | details | time   │
│    • Serialized via PostgreSQL advisory transaction locks                 │
└──────────────────────────────────────┬────────────────────────────────────┘
                                       │
═══════════════════════════════════════╪═════════════════════════════════════
                                       │
┌──────────────────────────────────────▼────────────────────────────────────┐
│                    3. PERSISTENCE & STORAGE LAYER                         │
│  • PostgreSQL / Neon: Users, Documents, Scans, Entities, Audit Logs       │
│  • Local Storage: AES-128 Encrypted Original and Redacted Blobs          │
└───────────────────────────────────────────────────────────────────────────┘
```

---

### Phase 1: Authentication & Access Control
1. **Password Security**: Passwords are hashed with **Argon2id**. New passwords must pass strict policy checks (min 10 characters, not repetitive, not in top blocklists, not containing email local parts).
2. **Timing Attack Protection**: When an email is not found, `burn_verify()` executes against a dummy hash to equalize response times and prevent user enumeration.
3. **Lockout Policy**: After 5 failed attempts, the account is locked for 15 minutes and all active sessions are revoked.
4. **Token Dual-Model**:
   - **Access Token**: Short-lived 15-minute JWT carrying user ID, role, and session family ID (`sid`).
   - **Refresh Token**: High-entropy cryptographically random string (48 bytes URL-safe) stored exclusively as a SHA-256 hash in the database and sent via an `HttpOnly`, `SameSite=lax` cookie.
5. **Token Rotation & Theft Detection**: Every refresh rotates the cookie. A 10-second grace period allows concurrent browser tabs to refresh cleanly. If a revoked token is used outside the grace window, the entire session family is revoked immediately.
6. **Instant Revocation**: Every incoming API call validates that the access token's session ID (`sid`) is still active in the database. Logging out or changing passwords invalidates access tokens immediately without waiting 15 minutes.

---

### Phase 2: File Upload & Ingestion
- Uploads are accepted via `POST /api/v1/documents`.
- **Magic Byte Verification**: File extensions are checked against true file signatures:
  - `%PDF-` for PDFs
  - `\x89PNG` for PNG images
  - `\xff\xd8\xff` for JPEGs
  - Zip package inspection (`word/document.xml`) for DOCX files
  - UTF-8 validation via `charset-normalizer` for plain text
- **Zip-Bomb Protection**: DOCX archives are capped at max 5,000 entries and 200MB uncompressed volume.
- **Encrypted Local Storage**: The uploaded file is saved to `storage/originals/{uuid}.ext` wrapped in Fernet encryption (`PIISENC1` file magic header).
- **Atomic Operations**: Stored using temporary `.part` files with atomic `os.replace` to prevent partial reads.

---

### Phase 3: Text & Layout Extraction
Extractors normalize every document into a **Canonical Text representation** with character indices (`0` to `N`) and a layout `page_map`:
- **PDF**: PyMuPDF (`fitz`) extracts words, bounding boxes, and rotation matrices. If a page has fewer than 20 characters of extractable text, it triggers **RapidOCR fallback** at 200 DPI, converting pixel coordinates into PDF point space.
- **DOCX**: Extracts structured paragraphs, table cells, headers, and footers recursively while recording XML path mappings.
- **Images**: RapidOCR runs text recognition and bounding box geometry.
- **Plain Text**: Strips UTF-8 BOM, normalizes CRLF to `\n`, and records single-page mappings.

---

### Phase 4: Dual-Engine PII Detection & Hybrid Merging

PII Sentinel runs two independent detection engines in parallel:

#### 1. Deterministic Rules Engine (`app/ml/rules.py` & `validators.py`)
- **Aadhaar Number**: 12-digit regex validated against the **Verhoeff checksum** algorithm. Checksum passes score 0.90 (0.97 with context keywords like *UIDAI*, *Aadhaar*).
- **PAN Card**: Pattern `[A-Z]{5}[0-9]{4}[A-Z]`. Validates the 4th letter against valid Income Tax entity codes (`ABCFGHLJPTK`). Score 0.85–0.95.
- **Phone Numbers**: Indian mobile numbers (10 digits starting with 6–9, with optional `+91` or `0` prefix). Boosted by context keywords within a 40-character lookback window.
- **Date of Birth**: Recognizes DMY, D-Mon-Y, Mon-D-Y, and ISO formats, validates calendar dates between 1900 and today, and strictly requires DOB context keywords.
- **Other Rules**: Passport, Voter ID (EPIC), Bank Account Numbers, IFSC codes, UPI IDs, Vehicle Registration (validates 36 Indian state/UT codes), Customer IDs, and Employee IDs.

#### 2. ML Transformer Engine (`app/ml/transformer.py`)
- Loads Hugging Face token-classification checkpoints (e.g. fine-tuned XLM-RoBERTa / MiniLM).
- **Sliding Window Inference**: Uses token stride (window 256, stride 64) with overlapping boundaries to process arbitrarily long documents without token truncation.
- **BIO Sequence Decoding**: Reconstructs continuous entity spans from `B-` and `I-` labels, calculates token-level confidence scores, and normalizes spans.

#### 3. Hybrid Conflict Resolution (`app/ml/hybrid.py`)
Candidates are resolved through a 4-step pipeline:
1. **Confidence Gate**: Drops any candidate below `MIN_CONFIDENCE` (default 0.3).
2. **Same-Type Union**: Overlapping detections of the same category merge into their union boundary `[min(start), max(end)]` with `score = max(scores)`.
3. **Cross-Type Greedy Resolution**: When overlapping spans differ in type, candidates are prioritized by `(-score, -length, method_rank, start)`. The deterministic rule engine (`method_rank=0`) wins over ML models on equal scores. Suppressed candidates are recorded in the winner's `provenance` metadata.
4. **Cap**: Output is capped at `MAX_ENTITIES_PER_SCAN` (default 5,000).

---

### Phase 5: Human-in-the-Loop Review & Delegation

- **Document Delegation**: A document owner can assign specific users holding the `REVIEWER` or `ADMIN` role to inspect a document (`POST /documents/{id}/reviewers`).
- **Interactive Review UI**:
  - Highlights all detected PII in the canonical document text view.
  - Reviewers can mark entities as:
    - **`REDACT`**: Include in redaction.
    - **`KEEP`**: Safe / whitelisted.
    - **`FALSE_POSITIVE`**: Detector error.
- **Bulk Action & Threshold Tuning**:
  - Reviewers can apply bulk decisions to all detections meeting a confidence threshold (e.g., *Redact all with confidence $\ge 0.85$*).
  - Includes a **"Re-apply to already reviewed items"** toggle (`only_pending=false`) so reviewers can freely adjust thresholds and reclassify previously evaluated items.
- **Manual Span Additions**:
  - Reviewers can highlight any text missed by the models and add it as a manual PII entity (`is_manual=true`).

---

### Phase 6: Multi-Format Physical Redaction

Redaction removes data permanently rather than visually covering it:
- **PDF Redaction**:
  - Applies PyMuPDF annotations with `page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)`.
  - Underlying vector text and raster image pixels beneath bounding boxes are **physically expunged** from the binary content stream.
  - Clears all metadata dictionaries, XML streams, form fields, and embedded annotations.
- **DOCX Redaction**:
  - Traverses the OpenXML DOM down to `<w:t>` text runs across body paragraphs, tables, headers, and footers.
  - Splits text runs at character boundaries, replaces redacted text with `[REDACTED]` or partial masks (`XXXX1234`), and preserves surrounding XML font and layout tags.
  - Clears document properties (author, company, edit time, revision history).
- **Image Redaction**:
  - Uses Pillow to draw solid black fill rectangles over bounding boxes.
  - Re-encodes the image and strips all EXIF metadata.
- **Plain Text Redaction**:
  - Replaces character offsets in memory and writes cleaned UTF-8 strings.

---

### Phase 7: Zero-Trust Leak Verification Gate

Before any redacted file is saved to storage:
1. The output document is re-opened and its entire text is re-extracted (including OCR on PDF/images and scanning all XML streams in DOCX).
2. The verification engine (`check_absent`) counts occurrences of every redacted needle in the original vs. redacted file.
3. If any redacted string appears in the output at a frequency higher than allowed, **verification fails**:
   - The generated file is deleted immediately.
   - The database records `validation_status = "FAILED"`.
   - The user is alerted that the document could not be verified automatically.

---

### Phase 8: Tamper-Evident Cryptographic Audit Logging

Every critical system action (logins, uploads, scans, reviewer decisions, downloads, and purges) is written to `audit_logs`:
- **SHA-256 Hash Chaining**:
  $$\text{row\_hash} = \text{SHA256}(\text{prev\_hash} \mid \text{seq} \mid \text{action} \mid \text{user\_id} \mid \text{doc\_id} \mid \text{details} \mid \text{timestamp})$$
- **Monotonic Advisory Locks**: Writes acquire `pg_advisory_xact_lock` in PostgreSQL, preventing race conditions or fork chains during concurrent inserts.
- **Verification Endpoint**: Admins can run `GET /api/v1/admin/audit-logs/verify` to re-calculate the cryptographic chain from row 1 to verify that no log entries were altered, deleted, or inserted out of order.

---

### Phase 9: Background Scans & Data Retention
- **Self-Healing Scans**: Scans claim documents atomically (`UPDATE scans SET status='PROCESSING' WHERE status='PENDING'`). On server reboot, any interrupted scans are marked `FAILED` gracefully rather than hanging forever.
- **Automated Retention Purge**: A background coroutine runs every `RETENTION_INTERVAL_HOURS` to delete expired documents, scan entities, and audit logs according to configured retention day limits.

---

## 4. Role-Based Access Control (RBAC)

| Permission / Action | `USER` | `REVIEWER` | `ADMIN` |
| :--- | :---: | :---: | :---: |
| Self-register account (if allowed) | ✅ | ❌ *(User only)* | ❌ *(User only)* |
| Upload & scan own documents | ✅ | ✅ | ✅ |
| Download own original document | ✅ | ✅ | ✅ |
| Delete own document | ✅ | ✅ | ✅ |
| Assign reviewer to own document | ✅ | ✅ | ✅ |
| **Inspect assigned documents queue** | ❌ | **✅** | **✅** |
| **Review & decide on PII entities** | Own only | **Assigned + Own** | **All / Assigned** |
| **Download assigned raw original file** | ❌ | **❌ *(Forbidden)*** | ❌ *(Owner only)* |
| **Download assigned redacted file** | ❌ | **✅** | **✅** |
| Manage users & change account roles | ❌ | ❌ | **✅** |
| View & verify cryptographic audit logs | ❌ | ❌ | **✅** |

---

## 5. Database Architecture & Data Models

```
┌─────────────────────────────────┐
│              User               │
├─────────────────────────────────┤
│ id: UUID (PK)                   │
│ name: String                    │
│ email: String (Unique)          │
│ password_hash: String (Argon2)  │
│ role: Enum (USER/REVIEWER/ADMIN)│
│ is_active: Boolean              │
│ failed_logins: Integer          │
│ locked_until: Timestamp         │
└──────────────┬──────────────────┘
               │ 1:N
               ▼
┌─────────────────────────────────┐       1:N       ┌─────────────────────────────────┐
│            Document             ├────────────────►│        DocumentReviewer         │
├─────────────────────────────────┤                 ├─────────────────────────────────┤
│ id: UUID (PK)                   │                 │ id: UUID (PK)                   │
│ uploaded_by: UUID (FK -> User)  │                 │ document_id: UUID (FK)          │
│ original_filename: String       │                 │ reviewer_id: UUID (FK -> User)  │
│ storage_key: String (Encrypted) │                 │ assigned_by: UUID (FK -> User)  │
│ file_type: String (pdf/docx/...)│                 └─────────────────────────────────┘
│ sha256: String                  │
│ status: Enum (UPLOADED/SCANNED) │
└──────────────┬──────────────────┘
               │ 1:N
               ▼
┌─────────────────────────────────┐       1:N       ┌─────────────────────────────────┐
│              Scan               ├────────────────►│            PiiEntity            │
├─────────────────────────────────┤                 ├─────────────────────────────────┤
│ id: UUID (PK)                   │                 │ id: UUID (PK)                   │
│ document_id: UUID (FK)          │                 │ scan_id: UUID (FK)              │
│ model_version: String           │                 │ entity_type: String (17 types)  │
│ detection_mode: String          │                 │ detected_value: EncryptedText   │
│ extracted_text: EncryptedText   │                 │ masked_value: String            │
│ page_map: JSON (Bboxes & layout)│                 │ confidence_score: Float (0-1)   │
│ status: PENDING/PROCESSING/DONE │                 │ start_position: Integer         │
└──────────────┬──────────────────┘                 │ end_position: Integer           │
               │ 1:N                                │ review_status: PENDING/REDACT.. │
               ▼                                    │ is_manual: Boolean              │
┌─────────────────────────────────┐                 └────────────────┬────────────────┘
│        RedactedDocument         │                                  │ 1:N
├─────────────────────────────────┤                                  ▼
│ id: UUID (PK)                   │                 ┌─────────────────────────────────┐
│ original_document_id: UUID (FK) │                 │        RedactionAction          │
│ source_scan_id: UUID (FK)       │                 ├─────────────────────────────────┤
│ storage_key: String             │                 │ id: UUID (PK)                   │
│ redaction_count: Integer        │                 │ entity_id: UUID (FK)            │
│ validation_status: PASSED/FAILED│                 │ decided_by: UUID (FK -> User)   │
└─────────────────────────────────┘                 │ action: REDACT/KEEP/FP          │
                                                    │ decided_at: Clock Timestamp     │
                                                    └─────────────────────────────────┘

┌─────────────────────────────────┐
│            AuditLog             │
├─────────────────────────────────┤
│ seq: BigInteger (Monotonic PK)  │
│ prev_hash: String (SHA-256)     │
│ row_hash: String (SHA-256)      │
│ user_id: UUID (Nullable)        │
│ document_id: UUID (No FK)       │
│ action: String                  │
│ details: JSON                   │
│ created_at: Timestamp           │
└─────────────────────────────────┘
```

---

## 6. Security & Cryptography Model

### 1. Application-Level MultiFernet Encryption
- Uses **Fernet** symmetric encryption (AES-128-CBC + HMAC-SHA256).
- **Key Rotation**: The `ENCRYPTION_KEYS` variable accepts a comma-separated list of keys. The first key encrypts all new data; all configured keys can decrypt.
- **Dual-Layer Coverage**:
  - **Storage Files**: Prepended with `PIISENC1` magic bytes.
  - **Database Fields**: SQLAlchemy `EncryptedText` TypeDecorator prepends `enc1:` and encrypts `Scan.extracted_text` and `PiiEntity.detected_value`.

### 2. Path Traversal & Injection Shields
- `LocalStorage.path_for(key)` ensures keys contain no leading slashes or `..` paths, verifying with `path.is_relative_to(self.root)`.
- SQL queries use SQLAlchemy parameterized bindings to prevent SQL injection.
- Security headers injected on every response:
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: no-referrer`
  - `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` (in production)
  - `Strict-Transport-Security: max-age=31536000; includeSubDomains` (in production)

---

## 7. API Route Reference

### Authentication (`/api/v1/auth`)
| Method | Path | Description | Access |
| :--- | :--- | :--- | :--- |
| `POST` | `/auth/register` | Register new user account | Public (if enabled) |
| `POST` | `/auth/login` | Authenticate and obtain JWT access token + refresh cookie | Public |
| `POST` | `/auth/refresh` | Rotate refresh token and issue new access token | Cookie |
| `POST` | `/auth/logout` | Revoke session family and delete cookie | Authenticated |
| `GET` | `/auth/me` | Fetch profile of authenticated user | Authenticated |
| `POST` | `/auth/change-password` | Change password with policy validation and session reset | Authenticated |

### Documents (`/api/v1/documents`)
| Method | Path | Description | Access |
| :--- | :--- | :--- | :--- |
| `POST` | `/documents` | Upload new document (multipart/form-data) | Authenticated |
| `GET` | `/documents` | List uploaded documents with pagination and status filters | Authenticated |
| `GET` | `/documents/{id}` | Get document metadata and scan history | Owner only |
| `GET` | `/documents/{id}/download`| Download original unredacted file | Owner only |
| `DELETE`| `/documents/{id}` | Delete document, scans, and stored files | Owner only |
| `POST` | `/documents/{id}/scan` | Trigger asynchronous PII scan | Owner only |
| `POST` | `/documents/{id}/reviewers` | Assign a reviewer to the document | Owner only |
| `GET` | `/documents/{id}/reviewers` | List assigned reviewers for the document | Owner only |
| `DELETE`| `/documents/{id}/reviewers/{uid}` | Remove reviewer from the document | Owner only |

### Reviews & Entities (`/api/v1`)
| Method | Path | Description | Access |
| :--- | :--- | :--- | :--- |
| `GET` | `/review/assigned` | List documents assigned to caller for review | Reviewer / Admin |
| `GET` | `/scans/{id}` | Get scan status and summary | Owner / Reviewer |
| `GET` | `/scans/{id}/text` | Get canonical extracted text and coordinates | Owner / Reviewer |
| `GET` | `/scans/{id}/entities` | List detected PII entities with confidence and review status | Owner / Reviewer |
| `POST` | `/entities/{id}/review` | Record review decision (`REDACT`, `KEEP`, `FALSE_POSITIVE`) | Owner / Reviewer |
| `PATCH`| `/entities/{id}` | Reclassify entity category type | Owner / Reviewer |
| `POST` | `/scans/{id}/reviews/bulk` | Bulk decision with confidence filter and override toggle | Owner / Reviewer |
| `POST` | `/scans/{id}/entities` | Add manual entity span missed by detector | Owner / Reviewer |
| `DELETE`| `/entities/{id}` | Delete a manually created entity | Owner / Reviewer |

### Redaction (`/api/v1`)
| Method | Path | Description | Access |
| :--- | :--- | :--- | :--- |
| `POST` | `/scans/{id}/redact` | Run physical redaction and verification gate | Owner / Reviewer |
| `GET` | `/documents/{id}/redacted` | List generated redacted outputs for a document | Owner / Reviewer |
| `GET` | `/redacted-documents/{id}/download` | Download verified redacted document | Owner / Reviewer |
| `DELETE`| `/redacted-documents/{id}` | Delete a redacted output file | Owner / Reviewer |

### Administration (`/api/v1/admin`)
| Method | Path | Description | Access |
| :--- | :--- | :--- | :--- |
| `GET` | `/admin/users` | List all users across the organization | Admin only |
| `POST` | `/admin/users` | Create user with explicit role (`USER`, `REVIEWER`, `ADMIN`)| Admin only |
| `PATCH`| `/admin/users/{id}` | Update role, active status, reset password, unlock | Admin only |
| `GET` | `/admin/audit-logs` | Query append-only audit trail with filtering | Admin only |
| `GET` | `/admin/audit-logs/verify` | Re-verify cryptographic SHA-256 hash chain integrity | Admin only |

---

## 8. Configuration & Environment Variables

Configure via `.env` in the project root:

```ini
# ---- Environment & Security ----
ENVIRONMENT=development                       # "development" or "production"
SECRET_KEY=change-me-in-env                   # >= 32 characters in production
DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname?sslmode=require
ENCRYPTION_KEYS=                              # Comma-separated Fernet keys (required in prod)

# ---- Web & CORS ----
CORS_ORIGINS=http://localhost:5173            # Exact public origins of the frontend
ACCESS_TOKEN_MINUTES=15                       # Access token TTL
REFRESH_TOKEN_DAYS=7                          # Refresh token TTL
RATE_LIMIT_STORAGE_URI=memory://              # Or redis://host:6379 for distributed limits

# ---- Account Policies ----
ALLOW_REGISTRATION=true                       # false to force admin-only user provisioning
MAX_FAILED_LOGINS=5                           # Lock account after N failures
LOCKOUT_MINUTES=15                            # Lockout duration

# ---- Storage & Limits ----
STORAGE_DIR=c:\pii_project\PII_Sentinel\storage
MAX_UPLOAD_MB=20                              # Maximum upload file size
MAX_PDF_PAGES=200                             # PDF page limit
SCAN_TIMEOUT_SECONDS=120                      # Scan job deadline
MAX_EXTRACTED_CHARS=2000000                   # Extraction character cap

# ---- Detection & ML ----
MIN_CONFIDENCE=0.3                            # Minimum threshold to save an entity
NER_MODEL=                                    # Model path/HF ID (empty = rules-only)
NER_LOCAL_ONLY=true                           # Block HuggingFace network calls
NER_MIN_SCORE=0.5                             # Model token threshold
NER_MAX_CHARS=200000                          # Text ceiling evaluated by NER
MAX_ENTITIES_PER_SCAN=5000                    # Safety cap per document

# ---- OCR Engine ----
OCR_DPI=200                                   # Scanned PDF rasterization resolution
OCR_MIN_TEXT_CHARS=20                         # Trigger OCR if page text < 20 chars
OCR_WARMUP=true                               # Preload models in background thread

# ---- Retention Purge ----
RETENTION_DAYS=0                              # Delete documents older than N days (0=disabled)
AUDIT_RETENTION_DAYS=0                        # Delete audit logs older than N days (0=disabled)
RETENTION_INTERVAL_HOURS=6                    # Purge loop frequency
```

---

## 9. Local Development & Deployment Guide

### Running Backend Locally with Live Neon PostgreSQL
```powershell
# 1. Navigate to backend directory
cd c:\pii_project\PII_Sentinel\backend

# 2. Activate virtual environment
.\myenv\Scripts\Activate.ps1

# 3. Apply database migrations to Neon DB
python -m alembic upgrade head

# 4. Create or update your Admin account
$env:ADMIN_EMAIL = "admin@example.com"
$env:ADMIN_PASSWORD = "YourSecretPassword123"
python -m scripts.create_admin

# 5. Start the FastAPI server (Port 8001)
uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

### Running Frontend Locally
```powershell
# In a separate terminal
cd c:\pii_project\PII_Sentinel\frontend

# Install dependencies (if first time)
npm install

# Start Vite dev server (Port 5173)
npm run dev
```

Visit **`http://localhost:5173`** in your browser. All API requests to `/api/*` are reverse-proxied directly to `http://127.0.0.1:8001`.
