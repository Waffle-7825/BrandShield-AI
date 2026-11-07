# BrandShield AI — Round 2 working prototype

**Team:** VERITYFORGE  
**Participant:** Ashwatha U

## Implemented in this starter
- Brand knowledge library with text and PDF/DOCX/TXT/MD source ingestion.
- SQLite persistence for sources, reviews and reviewer events.
- Deterministic checks for configured avoid-terms.
- Heuristic lexical matching against explicitly approved facts.
- SUPPORTED / CONTRADICTED / UNVERIFIED findings with source name, rationale and suggested action.
- Human decisions: Approve, Request revision, Reject.
- Review history and recorded decision events.
- Responsive browser UI.

## Run locally (Windows PowerShell)
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload
```
If `python` is not recognised:
```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```
Open http://127.0.0.1:8000

## Demo reference syntax
One rule per line:
```text
Preferred term: members
Avoid term: customers
Approved fact: Our platform helps small businesses organise customer conversations in one workspace.
Approved fact: The platform is available on web and mobile.
```

## Honest technical limitations
- The review input is pasted text. PDF/DOCX/TXT/MD parsing is currently available for source-library ingestion.
- The engine is deterministic + lexical overlap. It is not an LLM, not a trained model and does not prove truth.
- No authentication or multi-user roles in this prototype.
- No visual/logo or accessibility analysis yet.
- SQLite local persistence is suitable for a demo; public deployment needs persistent storage and access controls before confidential content is uploaded.
- Automated findings must be checked by an authorised human.

## Deployment
Deploy as a Python web service with the start command:
`uvicorn main:app --host 0.0.0.0 --port $PORT`
Use a persistent disk/volume for the `data/` directory if the host supports it.
