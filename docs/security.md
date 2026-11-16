# Security and privacy

This is a university project. The controls below are real and tested, but the application has **not** had an independent
security assessment or penetration test, and nothing here is a compliance claim (GDPR, DPDP Act, HIPAA, SOC 2, ...).

## Controls in place
| Area | What exists |
|---|---|
| Passwords | Argon2id; minimum 10 characters; constant-time-ish login (a dummy hash is verified for unknown emails); password policy: rejects common passwords, passwords containing the email local part, and trivially simple values |
| Sessions | 15-minute JWT access token in memory only (never in storage or a cookie); refresh token in an `HttpOnly`, `SameSite=Lax`, path-scoped cookie (`Secure` outside development), stored only as a SHA-256 hash; rotated on every use; access tokens are tied to a login family and die on logout, expiry or admin disable; replay of an old refresh token ends that login |
| Account lockout | After `MAX_FAILED_LOGINS` (default 5) consecutive wrong passwords the account is locked for `LOCKOUT_MINUTES` (default 15); all existing sessions are revoked; the admin page shows an **Unlock** button |
| Registration | `ALLOW_REGISTRATION` (default `true`) can be set `false` to disable self-registration; the frontend hides the Register button automatically; admins can create accounts via `POST /api/v1/users` regardless |
| Change password | `POST /api/v1/auth/change-password` (current password + new password); revokes all existing sessions; admin can also reset via `PATCH /api/v1/users/{id}` with `new_password` |
| Authorisation | Enforced on the server for every route (tested by enumerating the OpenAPI schema). Owner-only documents; assigned reviewers can review and redact but not download, delete or re-scan; admins manage users and read the audit log but get no blanket document access; other users get 404 (no existence leak) |
| Identity | Always derived from the verified token; the role is read from the database each request, never from the token |
| Uploads | Extension, declared type and magic-byte checks; size limit enforced while reading and via `Content-Length`; DOCX zip-bomb guard; image pixel limit; PDF page limit; server-generated storage keys; path-traversal-safe storage; files never served from a public path |
| Downloads | `attachment`, `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store` |
| Browser | CORS limited to configured origins and methods; cookie endpoints refuse foreign `Origin`; `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`; outside development also CSP and HSTS and no API docs; document text is rendered as plain text (no `innerHTML`) |
| Abuse | Rate limits on login/register/refresh (10/min) and on upload, scan and redact (60/min), per client address; `Content-Length` required for streamed bodies |
| Errors and logs | Unhandled errors return a generic 500; validation errors do not echo values; audit logs and application logs hold ids, types and counts, never detected values, passwords or tokens |
| Secrets | From environment / untracked `.env`; the app refuses to start outside development with a missing, default or short `SECRET_KEY` |
| Encryption at rest | Uploaded and redacted files are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) by `app/security/crypto.py`. `Scan.extracted_text` and `PiiEntity.detected_value` use an `EncryptedText` SQLAlchemy column type (same on-disk format, transparent read/write). `ENCRYPTION_KEYS` is required outside development; use `scripts/encrypt_existing.py` to migrate existing plaintext rows. Key rotation: add a new key at front, run with `--rotate`, then remove the old key |
| Redaction | Text and pixels are removed from the new file (not covered); metadata scrubbed; each output re-read and checked; a failed check keeps no file; the original is never modified |
| Audit log | Tamper-evident SHA-256 hash chain: each row stores `seq`, `prev_hash`, and `row_hash`; verify integrity with `GET /api/v1/audit-logs/verify` (admin only) or `scripts/verify_audit.py`. The chain detects row edits and out-of-order deletes; a full rewrite by a DB admin cannot be detected without an external write-once copy |
| Retention | `RETENTION_DAYS` (default 0 = keep forever) automatically purges old documents and their associated files/scans/entities; `AUDIT_RETENTION_DAYS` similarly purges old audit rows; trimming the oldest rows is allowed and does not break the chain verification |
| Dependencies | `pip-audit` and `npm audit`: no known vulnerabilities at the time of the last run |

## Residual risks and gaps (read before using real documents)
1. **Encryption at rest is application-level only.** Use full-disk and database encryption in addition; restrict database access; treat the database like the documents themselves. The `original_filename` column, page bounding boxes, and audit `details` fields are stored without encryption.
2. **No automatic retention by default.** Set `RETENTION_DAYS` and `AUDIT_RETENTION_DAYS` in production. Deleted files are not securely wiped (encryption is the mitigation).
3. **Audit log tamper evidence has limits.** Someone with full database write access can recompute the entire chain. Ship audit logs to an external write-once store for real assurance.
4. **No MFA.** TOTP-based MFA is planned but not implemented. Password + rate-limiting is the only authentication factor.
5. **No email verification or self-service password reset.** Admins can reset passwords; password reset by email requires an SMTP service which is not built.
6. **Parsing untrusted files happens inside the web process** (PyMuPDF, python-docx, Pillow, ONNX OCR). Size limits help, but a parser vulnerability would run with the application's privileges. There is no malware scanning and no sandbox.
7. **Rate limits are in memory, per process and keyed on the client address.** Behind a proxy configure trusted forwarded addresses; set `RATE_LIMIT_STORAGE_URI=redis://...` to share limits across multiple hosts.
8. **CSRF** relies on `SameSite=Lax` plus the `Origin` check; there is no separate CSRF token.
9. **Transport security is not provided by the app.** Terminate TLS in front of it; only then does the `Secure` cookie flag help.
10. **Detection is imperfect** (`docs/ml-pipeline.md`): missed PII stays in a "redacted" file. Review is mandatory by design.
11. **Redaction coverage:** DOCX comments, footnotes and text boxes are not edited (verification fails if a redacted value is still there); PDF form fields and annotations beyond the scrub are untested; scanned-page boxes are estimates, so verification re-OCRs the output; the `partial` text mask deliberately leaves the last four digits.
12. The optional LLM-assisted analysis is **not implemented**, so no document content leaves the machine.
13. Development defaults (`.env` with a generated key, seeded demo accounts) must not be used in production. The seed script refuses to run outside `ENVIRONMENT=development`.

## Reporting
Found a problem? Open an issue without including real personal data.
