# Round 3 change log

- Added explainable token/bigram cosine retrieval and optional `Equivalent term:` mappings.
- Added source versioning, source archive state, and historical source listing.
- Added possible polarity-conflict candidate detection.
- Added optional signed-cookie authentication, PBKDF2 password hashing, and admin/editor/reviewer permissions.
- Added workspace governance analytics and a small labelled precision/recall smoke-evaluation endpoint.
- Added admin-protected review-history clearing and reviewer actor tracking.
- Added API smoke tests and deployment/security notes.

## Validation performed for this package

- Python compilation: passed.
- Five dependency-light smoke tests: passed.
- JavaScript syntax check with Node: passed.
- Uvicorn startup + `/api/health` + homepage smoke check: passed.
- Authenticated-mode check: admin login, reviewer creation, and editor/reviewer permission denial: passed.

This is local package validation, not a claim that the hosted Render deployment has already been updated or tested.
