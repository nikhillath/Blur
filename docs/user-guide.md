# PII Sentinel: user guide

PII Sentinel finds personal information (names, phone numbers, ID numbers, emails, addresses...) in documents, lets you
**review** what it found, and then writes a **new copy** of the document with the information you chose removed.
Your original is never changed.

> **The most important thing to know.** Detection is automatic and imperfect. It misses some personal data and flags
> some things that are not personal data. A redacted file is only as safe as your review. Always check the result before
> you share it.

Contents: [1 Try it locally](#1-try-it-locally) · [2 Accounts and roles](#2-accounts-and-roles) · [3 The workflow in 8 steps](#3-the-workflow-in-8-steps) ·
[4 Pages](#4-pages-one-by-one) · [5 Understanding detections](#5-understanding-detections) · [6 Reviewing well](#6-reviewing-well) ·
[7 Redaction in detail](#7-redaction-in-detail) · [8 Working with a reviewer](#8-working-with-a-reviewer) · [9 Administrators](#9-administrators) ·
[10 Limits](#10-limits) · [11 Privacy habits](#11-privacy-habits) · [12 Troubleshooting](#12-troubleshooting) · [13 API reference](#13-api-quick-reference)

---

## 1. Try it locally

To run it for a demo on your own computer (for a real server see `docs/deployment.md`). You need Python 3.12, Node 20+ and PostgreSQL.

```powershell
# 1. database (once) - run in psql as a superuser
CREATE ROLE pii_sentinel LOGIN PASSWORD 'choose-a-password';
CREATE DATABASE pii_sentinel OWNER pii_sentinel;

# 2. backend (from the project folder)
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install torch --index-url https://download.pytorch.org/whl/cpu     # skip for rules-only mode
pip install -r requirements.txt

# 3. settings: copy ..\.env.example to ..\.env and set at least
#    ENVIRONMENT=development, DATABASE_URL=..., CORS_ORIGINS=http://localhost:5173
#    NER_MODEL=<full path to ml\artifacts\pii-minilm-quick\best>   (leave empty for rules-only)
alembic upgrade head
python -m scripts.seed          # creates admin@ / reviewer@ / user@example.com and prints their shared password ONCE
uvicorn app.main:app --reload --port 8000

# 4. frontend (second terminal)
cd frontend
npm install
npm run dev                     # open http://localhost:5173
```

Sample fictional documents to try are in `tests/fixtures/` (`sample.txt`, `sample.pdf`, `sample.docx`).
The seeded accounts are for local demos only: never create them on a real server.

## 2. Accounts and roles

Create an account with **Register** (name, email, password of at least 10 characters). New accounts are always `USER`.

| Role | Can do |
|---|---|
| **USER** | Upload documents, scan them, review and redact their **own** documents, download, delete, assign reviewers |
| **REVIEWER** | Everything a user can for their own documents, **plus** review and redact documents other people assigned to them. For those documents they cannot download the original, delete, re-scan, or open the document page |
| **ADMIN** | Everything a reviewer can, **plus** the Admin page (manage users, read the audit log). Admins do **not** get automatic access to other people's documents |

Only an administrator can promote someone to REVIEWER or ADMIN (Admin page). You stay signed in while you use the app
(the session renews itself) and until you press **Log out**.

## 3. The workflow in 8 steps

1. **Upload.** Click **Upload**, drag a file in (or **Browse**). Leave "Scan for personal data right after upload" ticked.
2. **Scan.** You land on the document page. The scan runs in the background ("Scan running..."); the status becomes `COMPLETED`.
3. **Open the review.** In the **Scans** table click **Review**.
4. **Read the suggestions.** Highlighted text in the document, and a list on the right: each shows the type, a masked value
   (like `r***@example.com`), the confidence and how it was found. They all start as *model suggestion*.
5. **Decide.** For each item choose **Redact**, **Keep**, or **False positive**. For many at once use **Bulk action**.
   If the detector missed something, select the text in the document and **Add and redact**.
6. **Create the redacted file.** Press **Redact N item(s)**. A confirmation tells you how many items are still undecided
   (those stay in the file). Confirm.
7. **Check the verification.** The result shows **Verification: PASSED / SKIPPED / FAILED** (see section 7).
8. **Download.** Press **Download redacted file** and look through it before sharing.

## 4. Pages, one by one

**Landing and login.** Explains the workflow; **Log in** / **Create account**.

**Dashboard.** Numbers about *your* documents: Documents, Scanned, Awaiting review (documents whose latest scan still has
undecided items), Entities to review, Failed scans, Redacted files. Below: documents assigned to you for review (if you are
a reviewer) and your recent activity.

**Upload.** Drag and drop or browse; progress bar. Accepted: PDF, DOCX, TXT, PNG, JPEG. A rejected file shows the reason
(unsupported type, content does not match its extension, too large, empty).

**Documents.** Your documents with search (by file name), status filter, paging and **Delete**.
Status: `UPLOADED` (not scanned), `PROCESSING`, `SCANNED`, `FAILED` (no successful scan). The *Latest scan* column shows how many
detections the latest scan found.

**Document page.**
* **Scan for PII**: start another scan (earlier scans and their decisions are kept as history). Only one scan can run at a time.
* **Download original**, **Delete** (removes the document, its scans, detections and redacted files; cannot be undone).
* **Scans** table: when, status, how it was detected (`rules-only` = rules only; `hybrid` = rules plus the trained model),
  number of entities, warnings (for example "low OCR confidence"), **Review** for completed scans and **Retry** for failed ones.
* **Redacted files**: every file you created, how many items were redacted, its verification result, **Download** and **Delete**.
* **Reviewers**: add people (by email) who may review this document, and remove them.

**Review page.** See section 6. Left: the extracted text with highlights. Right: Bulk action, the detection list with filters,
and the panel to create the redacted file. The header counts Pending / Redact / Keep / False positive.

**Admin** (admins only). Users: change a role, enable/disable an account. Audit log: who did what and when; filter by action.

## 5. Understanding detections

### What gets detected

| Type | What it is | How it is found |
|---|---|---|
| PERSON | A person's name | Model; also a "Name:" label |
| ADDRESS | A street address | Model (cities score lower); also an "Address:" label |
| EMAIL | Email address | Rule |
| PHONE | Indian mobile number (with or without +91) | Rule + model |
| AADHAAR | 12-digit Aadhaar number | Rule (checks the official checksum) + model |
| PAN | Permanent Account Number | Rule + model |
| PASSPORT, VOTER_ID | Passport / voter ID number | Rule (only when the word "passport" / "voter" is nearby) + model |
| DRIVING_LICENSE | Driving licence number | Rule + model |
| VEHICLE_REG | Vehicle registration plate | Rule + model |
| DATE_OF_BIRTH | A date of birth | Rule (only after a "DOB / Date of birth" keyword) + model |
| BANK_ACCOUNT | Bank account number | Rule (only with "account"/"A/c" nearby) |
| IFSC | Bank branch code | Rule |
| UPI_ID | UPI address like `name@bank` | Rule + model |
| CUSTOMER_ID, EMPLOYEE_ID | Ordinary business IDs after a label like "Customer ID:" | Label rule |
| OTHER_IDENTIFIER | Other IDs (GSTIN, medical ids, account ids, API keys...) | Mostly the model |

Government IDs (Aadhaar, PAN, passport, voter ID, licence) are kept separate from ordinary customer/employee IDs.

### How it was found (shown on each item)
`rule` (a pattern with checks) · `label-rule` (text after a label such as "Customer Name:") · `transformer` (the trained language model) ·
`hybrid` (rules and model agreed or overlapped) · `manual` (you added it).

### Confidence
The percentage is how strongly the rule or model believes in the item. It is **not** a probability: 90% does not mean "right 9 times
in 10". Use it to decide where to look first. Items under 60% are labelled **low confidence** and deserve a careful look. Even
99% items can be wrong, and anything the detector never found is invisible in the list, so read the document too.

### Review status
* **model suggestion** (status PENDING): nobody has decided yet.
* **human decision** (REDACT / KEEP / FALSE POSITIVE): a person decided. Only REDACT items are removed.

### Where it is weaker (measured on synthetic test data; see `docs/ml-pipeline.md`)
* Best on English, Hinglish and clean text. Hindi in Devanagari is noticeably weaker; other Indian languages are not reliably supported.
* OCR text (scans, photos) is less reliable: check the "low OCR confidence" warning.
* Vehicle numbers, ordinary IDs and "other identifiers" give the most false alarms; very short or oddly formatted values get missed.
* A scan with no model (`rules-only`) catches structured IDs well (emails, PAN, Aadhaar...) but misses most names and addresses.

## 6. Reviewing well

**The review page.**
* Click a highlight to select the matching item. Colours: pink = redact, green = keep, grey = false positive, yellow = undecided.
* Where two detections overlap, only the first is highlighted; the other is still in the list.
* **Filters** above the list: by type or by status. Use *Pending* to see what is left.

**Decisions.**
* **Redact**: remove this from the output.
* **Keep**: it is personal data-looking but you want it to stay (for example a public company phone number).
* **False positive**: the detector was wrong (it is not personal data). It stays in the output, and the decision is recorded.
* Changed your mind? Press another button; every decision is saved in a history (kept for the audit trail), the latest one counts.
* **Type dropdown** on an item: fix a wrong type (for example PHONE to BANK_ACCOUNT). The original type is remembered.

**Bulk action.** Pick an action and a confidence threshold, then **Apply**: it affects **only undecided items** with confidence at or
above the threshold. Handy sequence: Redact everything ≥ 0.9, then review the remaining ones by hand, mark the wrong ones
False positive, and finally bulk-redact the rest.

**Adding something that was missed.** Select the text in the document with the mouse, choose its type in the box that appears,
press **Add and redact**. (Spaces at the ends of the selection are trimmed; the span can be at most 500 characters.) Added items show a
**Remove** button.

**Before you create the file.** Aim for **Pending = 0**. The button reads `Redact N item(s)` and the confirmation tells you how many
items are still undecided.

## 7. Redaction in detail

Press **Redact N item(s)**. The app builds a new file from the items currently marked Redact (overlapping items are merged),
checks it, and stores it. You can create several redacted files from the same scan (for example with different decisions); each is
listed on the document page.

**Text masking** (TXT and DOCX only): **Full** replaces each item with `[REDACTED]`; **Keep last 4 digits** shows e.g.
`XXXXXXXX9123` for numeric IDs (names, emails, addresses are still fully masked). Be aware that this *deliberately reveals 4
digits*. PDFs and images are always completely blacked out.

| File | What happens | Good to know |
|---|---|---|
| **PDF** | The text and the pixels under each box are removed from the file (not just covered); the file's metadata is cleared | Whole words that touch a detected span are removed, so a little neighbouring text can go too. Scanned pages: boxes come from OCR and are padded. Password-protected PDFs are refused |
| **DOCX** | Text is replaced inside the document; formatting elsewhere is kept; author and other properties are cleared | Covers body text, tables, headers and footers. Comments, footnotes and text boxes are **not** edited |
| **TXT** | Text is replaced | Line endings become LF, and a byte-order mark is dropped |
| **PNG / JPEG** | Black boxes are painted over the text; metadata (EXIF) is dropped | The file is re-encoded |

**Verification.** After building the file the app reopens it and checks that the redacted values are gone (for scanned pages it
re-reads the pixels with OCR; for DOCX it checks every XML part of the package). The result:

| Result | Meaning | What to do |
|---|---|---|
| **PASSED** | The checks found no redacted value left, metadata is clean, the file opens and has the same page count | Download, then do your own quick look |
| **SKIPPED** | The file was made but could not be checked automatically (an image, and OCR was unavailable) | Inspect it manually before sharing |
| **FAILED** | A redacted value was still present (or a check failed). **The file is not kept and cannot be downloaded** | Typical cause: the value also appears somewhere the app cannot edit (DOCX comment/footnote). Remove it in the original, upload again |

PASSED means "none of the *selected* values is left". It cannot tell you that something sensitive was **never detected**.

**Manual check (recommended).** Open the file, search for a few known values (name, phone, ID), look at every page including
headers/footers, and for PDFs try selecting text and searching, then zoom into the black boxes.

## 8. Working with a reviewer

1. On the **document page**, under **Reviewers**, enter the reviewer's email and **Add reviewer**. The person must already have an
   active account with the REVIEWER (or ADMIN) role (an admin can promote them). Plain users cannot be added.
2. The reviewer sees the document under **Assigned to you for review** on their dashboard, opens **Review**, and decides on items,
   adds missed text, bulk-actions and can create redacted files.
3. You (the owner) see their decisions (they are marked as human decisions, and the history records who decided).
4. Remove the reviewer when finished: access ends immediately. Deleting the document also removes the assignment.

A reviewer **cannot** download the original, delete, re-scan or reassign. The reviewer *can* read the document's extracted text
while reviewing, so assign only people who are allowed to see it.

## 9. Administrators

* **Users**: change roles (USER / REVIEWER / ADMIN), disable or enable accounts. A disabled user is signed out and cannot log in. You
  cannot change your own role or disable yourself.
* **Audit log**: every sign-in, failed sign-in, upload, scan, review decision, redaction, download and admin change. Filter by an action name.
  Examples: `LOGIN_FAILED`, `DOCUMENT_UPLOADED`, `SCAN_COMPLETED`, `SCAN_FAILED`, `ENTITY_REVIEWED`, `ENTITIES_BULK_REVIEWED`,
  `ENTITY_MANUAL_ADDED`, `ENTITY_TYPE_CORRECTED`, `REVIEWER_ASSIGNED`, `REDACTION_CREATED`, `REDACTION_FAILED_VALIDATION`,
  `REDACTED_DOWNLOADED`, `DOCUMENT_DELETED`, `ADMIN_USER_UPDATED`, `REFRESH_REUSE_DETECTED` (a possible stolen login: that login was ended).
  The log never contains the personal values themselves, only ids, types and counts.
* An admin cannot read other people's documents. To remove a user's data, ask the user to delete their documents (there is no admin delete).

## 10. Limits

| | Default |
|---|---|
| File types | PDF, DOCX, TXT, PNG, JPEG (checked by content, not just by name) |
| File size | 20 MB (set by the server administrator) |
| PDF pages | 200 |
| Extracted text | 2,000,000 characters; the language model reads the first 200,000 (the scan shows `ner_truncated_at_chars` if cut) |
| One scan at a time per document; scans time out after 120 s | |
| Not supported | Password-protected PDFs, `.doc` (old Word), `.rtf`, spreadsheets, HTML, archives |

## 11. Privacy habits

* Use fictional data to practise. Do not upload real identity documents to a demo installation.
* Delete documents you no longer need. Deleting removes the files and all detections; backups made earlier still hold them.
* The extracted text and the detected values are stored in the database in clear text. Whoever can read the database can read them.
* Do not share a redacted file until you have checked it (section 7).
* Log out on shared computers. Report anything odd to your administrator (for example `LOGIN_FAILED` entries you did not cause).

## 12. Troubleshooting

| Problem | What to do |
|---|---|
| "unsupported file type" / "content does not match its extension" | Use PDF, DOCX, TXT, PNG or JPEG; do not rename other formats |
| "the PDF could not be read" | The file is damaged or not a real PDF. Re-export it |
| "password-protected PDFs are not supported" | Remove the password first |
| "no extractable text was found" | A blank document, or a scan too poor for OCR. Rescan at higher quality |
| Scan `FAILED` | Read the message on the document page, fix the cause, press **Retry** (a new scan; the failed one stays in the history) |
| Scan says `rules-only` but you expected the model | The server has no model configured, or it failed to load. Ask your administrator (see `docs/deployment.md`) |
| Names and addresses are not detected | Same as above: `rules-only` mode finds few names. Add them by selecting text, or use the model |
| Many false alarms on numbers | Mark them **False positive** (bulk: filter by type, review, then act) |
| Verification **FAILED** | See section 7; remove the value from places the app cannot edit, upload again |
| "document not found" for a document you think exists | It belongs to someone else, or was deleted. You only see your own and assigned documents |
| Signed out unexpectedly after 15 minutes (server admins) | `CORS_ORIGINS` is wrong; see `docs/deployment.md` |
| Review page is slow on a huge document | The text view renders everything; split very large documents |
| Redacted PDF text looks different around the boxes | Whole words touching a detected span are removed; that is expected |

## 13. API quick reference

Everything in the app is available over HTTP (JSON) under `/api/v1`. The interactive docs are at `/docs` on a development server
(`ENVIRONMENT=development`); they are hidden in production. Authenticate with a short-lived token:

```bash
BASE=http://localhost:8000/api/v1
TOKEN=$(curl -s -X POST $BASE/auth/login -H 'content-type: application/json' \
        -d '{"email":"user@example.com","password":"..."}' | python -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
H="Authorization: Bearer $TOKEN"        # tokens last 15 minutes; browsers renew them with a refresh cookie

# upload, scan, wait for COMPLETED
DOC=$(curl -s -F "file=@sample.txt" -H "$H" $BASE/documents | python -c "import json,sys;print(json.load(sys.stdin)['id'])")
SCAN=$(curl -s -X POST -H "$H" $BASE/documents/$DOC/scan | python -c "import json,sys;print(json.load(sys.stdin)['id'])")
curl -s -H "$H" $BASE/scans/$SCAN                                   # status, detection_mode, entity_count

# read results (masked values only) and the extracted text
curl -s -H "$H" "$BASE/scans/$SCAN/entities?page_size=200"
curl -s -H "$H" $BASE/scans/$SCAN/text

# decide: one item, or in bulk
curl -s -X POST -H "$H" -H 'content-type: application/json' -d '{"action":"REDACT"}' $BASE/entities/<ENTITY_ID>/review
curl -s -X POST -H "$H" -H 'content-type: application/json' -d '{"action":"REDACT","min_confidence":0.9}' $BASE/scans/$SCAN/reviews/bulk

# redact and download
RID=$(curl -s -X POST -H "$H" -H 'content-type: application/json' -d '{"mask_style":"full"}' $BASE/scans/$SCAN/redact | python -c "import json,sys;print(json.load(sys.stdin)['id'])")
curl -s -H "$H" -o redacted.txt $BASE/redacted-documents/$RID/download
```

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/register`, `/auth/login`, `/auth/refresh`, `/auth/logout`, `GET /auth/me` |
| Documents | `POST /documents`, `GET /documents` (paging, `status`, `q`), `GET/DELETE /documents/{id}`, `GET /documents/{id}/download`, `POST /documents/{id}/scan`, `GET /documents/{id}/scans`, `GET/POST /documents/{id}/reviewers`, `DELETE /documents/{id}/reviewers/{user_id}`, `GET /documents/{id}/redacted` |
| Scans | `GET /scans/{id}`, `/scans/{id}/text`, `/scans/{id}/entities` (filters `entity_type`, `review_status`, `min_confidence`), `POST /scans/{id}/retry` |
| Review | `POST /entities/{id}/review`, `PATCH /entities/{id}` (type), `GET /entities/{id}/history`, `DELETE /entities/{id}` (manual only), `POST /scans/{id}/reviews/bulk`, `POST /scans/{id}/entities` (manual span: `start`, `end`, `entity_type`, character offsets into the extracted text), `GET /review/assigned` |
| Redaction | `POST /scans/{id}/redact`, `GET /redacted-documents/{id}`, `/redacted-documents/{id}/download`, `DELETE /redacted-documents/{id}` |
| Other | `GET /dashboard`; admin: `GET /users`, `PATCH /users/{id}`, `GET /audit-logs`; health: `GET /health/live`, `/health/ready` |

Errors: `401` not signed in, `403` not allowed (role) or untrusted origin, `404` not found **or not yours**, `409` conflict (for
example a scan already running), `413` too large, `415` unsupported type, `422` invalid input, `429` too many requests.

Offsets: every start/end position in the API counts characters in the *extracted text* returned by `/scans/{id}/text`, not in the original file.
