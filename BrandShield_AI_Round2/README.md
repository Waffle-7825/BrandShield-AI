# BrandShield AI — Round 3 build (VERITYFORGE)

BrandShield AI is an evidence-first brand governance workspace. It compares submitted text with approved reference material, surfaces possible issues, and keeps the final decision with a human reviewer.

## Round 3 improvements in this build

### 1. Evidence intelligence
- Hybrid retrieval uses token and phrase (bigram) cosine similarity, configurable equivalent-phrase expansion, and deterministic preferred/avoid-term checks.
- Each finding records the matched source ID, source version, line reference, and similarity score where applicable.
- Re-uploading a source with the same name creates a new version and archives the previous active version; older versions remain in the source history endpoint.
- A polarity-overlap scan flags possible conflicts between active approved facts for human confirmation.
- **Important:** retrieval is explainable lexical/phrase similarity, not a neural embedding model and not proof that a claim is true. Similarity is evidence triage only.

### 2. Secure collaboration
- Optional authenticated mode with signed, expiring, HttpOnly session cookies.
- Passwords are salted and hashed using PBKDF2-HMAC-SHA256; passwords are never stored in plaintext.
- Roles: `admin`, `editor`, `reviewer`. Admins manage users and workspace; editors manage sources and create reviews; reviewers can inspect findings and record decisions.
- When authentication environment variables are absent, the app runs in clearly identified single-user demo mode. **Do not use demo mode for confidential or multi-user content.**

### 3. Governance analytics and evaluation
- Workspace counts for reviews, decisions, finding statuses, issue categories, source versions and recent activity.
- A small, explicit labelled smoke-evaluation set reports precision/recall for avoid-term and promotional-claim detectors.
- Metrics are limited to the included labelled examples; they are not general model-performance claims.

### Additional hardening
- Input size limit for review text, parameterised SQL, SQLite WAL mode, review/event actor tracking, source archiving, and protected history clearing.
- `/api/health` reports readiness and workspace counts.

## Local setup (Windows PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload
```

If `python` is not recognised, use:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Open http://127.0.0.1:8000. Run the smoke tests with `python tests_smoke.py` after installing requirements. Without auth environment variables the app runs in single-user demo mode and seeds an example brand guide if the active library is empty.

## Enable authenticated mode

Set these environment variables before starting the server. Use a unique random secret of at least 32 characters and a strong admin password of at least 12 characters. Do not commit real values to Git.

```powershell
$env:BRANDSHIELD_AUTH_SECRET="replace-with-a-long-random-secret"
$env:BRANDSHIELD_ADMIN_EMAIL="admin@example.com"
$env:BRANDSHIELD_ADMIN_PASSWORD="replace-with-a-strong-password"
python -m uvicorn main:app --reload
```

The first startup creates the configured admin account if it does not already exist. Add editors/reviewers through `POST /api/users` while signed in as admin. Example JSON:

```json
{"email":"reviewer@example.com","password":"use-a-unique-password-at-least-12-chars","role":"reviewer"}
```

The browser UI displays a sign-in screen when authenticated mode is enabled. Session cookies are Secure/HttpOnly and intended for HTTPS deployment.

## API overview

- `GET /api/health` — health and workspace counts
- `GET /api/auth/config`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`
- `GET/POST /api/users`, `PATCH /api/users/{id}/deactivate` — admin user management
- `GET /api/sources`, `GET /api/sources/versions`, `POST /api/sources`, `DELETE /api/sources/{id}`
- `POST /api/reviews`, `GET /api/reviews`, `GET /api/reviews/{id}`, `POST /api/reviews/{id}/decision`
- `GET /api/analytics`, `GET /api/evaluation`, `GET /api/conflicts`
- `POST /api/demo/seed`
- `DELETE /api/reviews` — admin-only clearing of review and event history; approved sources and users are preserved

## Render deployment checklist

1. Set Root Directory to `BrandShield_AI_Round2`.
2. Build command: `pip install -r requirements.txt`.
3. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`.
4. Add a persistent disk mounted at `/var/data` and set `BRANDSHIELD_DATA_DIR=/var/data`. This is necessary for SQLite data to survive deploys/restarts on hosts with ephemeral filesystems.
5. Set `BRANDSHIELD_AUTH_SECRET`, `BRANDSHIELD_ADMIN_EMAIL`, and `BRANDSHIELD_ADMIN_PASSWORD` in the host's secret environment settings before inviting reviewers.
6. Deploy, then verify `/api/health`, sign in, create a source, run a review, record a decision, and inspect analytics.

SQLite with a persistent disk is suitable for a small single-instance demo. For multiple application instances or heavier concurrent collaboration, migrate to managed PostgreSQL before production use.

## Honest limitations

- The review input is pasted text. PDF/DOCX/TXT/MD parsing is for reference-source ingestion.
- Retrieval is explainable token/bigram cosine similarity plus optional brand-curated equivalent phrases. It is not neural embedding retrieval, a generative model, or true contextual reasoning system.
- Conflict detection is a heuristic polarity/overlap candidate detector and can miss or over-flag nuanced contradictions.
- Evaluation uses a small hand-labelled smoke-test set. Expand and independently review the dataset before making accuracy claims.
- The prototype does not yet provide tenant isolation, SSO, MFA, audit-log export, or enterprise compliance certification.
- Human reviewers must verify all material claims before publication.

## Source rule syntax

One rule per line. Use `Equivalent term: registered account holders = members` to teach the retrieval layer an explicit, reviewable brand-specific equivalence:

```text
Preferred term: members
Avoid term: customers
Approved fact: Our platform helps small businesses organise customer conversations in one workspace.
Approved fact: The platform is available on web and mobile.
Equivalent term: registered account holders = members
```
