from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Response, Depends
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict
import sqlite3, uuid, json, re, io, os, hmac, hashlib, base64, math, secrets

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("BRANDSHIELD_DATA_DIR", str(ROOT / "data")))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "brandshield.sqlite3"
SESSION_SECRET = os.getenv("BRANDSHIELD_AUTH_SECRET", "")
ADMIN_EMAIL = os.getenv("BRANDSHIELD_ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("BRANDSHIELD_ADMIN_PASSWORD", "")
AUTH_ENABLED = bool(SESSION_SECRET and ADMIN_EMAIL and ADMIN_PASSWORD)
SESSION_COOKIE = "brandshield_session"
SESSION_HOURS = 12
app = FastAPI(title="BrandShield AI", version="3.0.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def connect():
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    return c


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id TEXT PRIMARY KEY,name TEXT,kind TEXT,content TEXT,created TEXT,
          version INTEGER NOT NULL DEFAULT 1,active INTEGER NOT NULL DEFAULT 1,
          supersedes_id TEXT
        );
        CREATE TABLE IF NOT EXISTS reviews(
          id TEXT PRIMARY KEY,title TEXT,content TEXT,findings TEXT,status TEXT,created TEXT,updated TEXT,
          created_by TEXT DEFAULT 'demo'
        );
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY,review_id TEXT,action TEXT,note TEXT,created TEXT,actor TEXT DEFAULT 'demo'
        );
        CREATE TABLE IF NOT EXISTS users(
          id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('admin','editor','reviewer')),created TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews(created);
        CREATE INDEX IF NOT EXISTS idx_events_review ON events(review_id);
        CREATE INDEX IF NOT EXISTS idx_sources_name ON sources(name,version);
        """)
        # Safe migrations for databases created by earlier Round 2 versions.
        migrations = {
          "sources": [("version", "INTEGER NOT NULL DEFAULT 1"), ("active", "INTEGER NOT NULL DEFAULT 1"), ("supersedes_id", "TEXT")],
          "reviews": [("created_by", "TEXT DEFAULT 'demo'")],
          "events": [("actor", "TEXT DEFAULT 'demo'")]
        }
        for table, cols in migrations.items():
            present = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols:
                if col not in present:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        if AUTH_ENABLED:
            row = c.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()
            if not row:
                c.execute("INSERT INTO users VALUES(?,?,?,?,?,1)",
                          (str(uuid.uuid4()), ADMIN_EMAIL, hash_password(ADMIN_PASSWORD), "admin", stamp()))


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_s, digest_s = stored.split("$", 1)
        salt = base64.urlsafe_b64decode(salt_s.encode())
        expected = base64.urlsafe_b64decode(digest_s.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def sign_session(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = hmac.new(SESSION_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")


def read_session(token: str):
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(SESSION_SECRET.encode(), body.encode(), hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if not hmac.compare_digest(expected, supplied): return None
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        data = json.loads(raw)
        if data.get("exp", 0) < int(datetime.now(timezone.utc).timestamp()): return None
        with connect() as c:
            user = c.execute("SELECT id,email,role,active FROM users WHERE id=?", (data.get("sub"),)).fetchone()
        if not user or not user["active"]: return None
        return dict(user)
    except Exception:
        return None


def current_user(request: Request):
    if not AUTH_ENABLED:
        return {"id": "demo", "email": "demo@brandshield.local", "role": "admin", "demo": True}
    token = request.cookies.get(SESSION_COOKIE)
    user = read_session(token) if token else None
    if not user: raise HTTPException(401, "Please sign in to continue.")
    return user


def require_roles(*roles):
    def check(user=Depends(current_user)):
        if user["role"] not in roles: raise HTTPException(403, "Your role does not have permission for this action.")
        return user
    return check


init_db()


def extract(filename, raw):
    ext = Path(filename).suffix.lower()
    if ext in (".txt", ".md"): return raw.decode("utf-8", errors="replace")
    if ext == ".pdf":
        import fitz
        with fitz.open(stream=raw, filetype="pdf") as doc:
            return "\n".join(f"[Page {i+1}] {p.get_text()}" for i,p in enumerate(doc))
    if ext == ".docx":
        from docx import Document
        d = Document(io.BytesIO(raw))
        return "\n".join(p.text for p in d.paragraphs)
    raise HTTPException(400, "Supported files: PDF, DOCX, TXT, MD")


def list_sources(active_only=True):
    with connect() as c:
        sql = "SELECT * FROM sources" + (" WHERE active=1" if active_only else "") + " ORDER BY name,version DESC"
        return [dict(x) for x in c.execute(sql)]


def words(s):
    return re.findall(r"[a-z0-9']+", (s or "").lower())


def vectorize(text, aliases=()):
    toks = words(text)
    feats = Counter(toks)
    feats.update(" ".join(toks[i:i+2]) for i in range(len(toks)-1))
    # Curated equivalent phrases make retrieval domain-aware without hiding the mapping.
    low = (text or "").lower()
    for group in aliases:
        if any(phrase.lower() in low for phrase in group):
            for phrase in group:
                feats.update(words(phrase))
    return feats


def cosine(a, b):
    if not a or not b: return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = math.sqrt(sum(v*v for v in a.values()))
    nb = math.sqrt(sum(v*v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def parse_rules(refs):
    preferred, avoid, facts, aliases = [], [], [], []
    for src in refs:
        for line_no, line in enumerate(src["content"].splitlines(), 1):
            line = line.strip()
            for prefix, bucket in (("preferred term:", preferred), ("avoid term:", avoid), ("approved fact:", facts), ("equivalent term:", aliases)):
                if line.lower().startswith(prefix):
                    value = line.split(":", 1)[1].strip()
                    if value:
                        if bucket is aliases:
                            group=[x.strip() for x in value.split("=") if x.strip()]
                            if len(group)>=2: aliases.append(group)
                        else: bucket.append((value, src, line_no))
    return preferred, avoid, facts, aliases


def detect_conflicts(facts):
    conflicts = []
    neg = {"not", "no", "never", "without", "cannot", "can't", "doesn't", "isn't", "aren't", "false"}
    for i, (a, sa, la) in enumerate(facts):
        wa = set(words(a)); na = bool(wa & neg)
        for b, sb, lb in facts[i+1:]:
            if sa["id"] == sb["id"] and la == lb: continue
            wb = set(words(b)); nb = bool(wb & neg)
            common = wa & wb - neg
            denom = max(1, min(len(wa - neg), len(wb - neg)))
            overlap = len(common) / denom
            if na != nb and overlap >= 0.65 and len(common) >= 3:
                conflicts.append({"fact_a": a, "source_a": sa["name"], "version_a": sa["version"],
                                  "fact_b": b, "source_b": sb["name"], "version_b": sb["version"],
                                  "similarity": round(overlap, 3), "type": "Possible polarity conflict"})
    return conflicts


def analyse(text):
    refs = list_sources(True)
    preferred, avoid, facts, aliases = parse_rules(refs)
    findings = []
    low = text.lower()
    for term, src, line_no in avoid:
        if re.search(r"(?<!\w)" + re.escape(term.lower()) + r"(?!\w)", low):
            replacements = [x[0] for x in preferred]
            findings.append({"severity":"High", "status":"CONTRADICTED", "category":"Terminology",
              "issue":f'The content uses “{term}”, which the brand reference says to avoid.',
              "evidence":f'{src["name"]} v{src["version"]} · line {line_no} · Avoid term: {term}',
              "source_id":src["id"], "source_version":src["version"], "match_score":1.0,
              "suggestion":("Consider the preferred term: " + ", ".join(replacements[:3])) if replacements else "Replace or remove this term according to the approved brand guidance."})
    chunks = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", text) if len(x.strip()) >= 12]
    fact_vectors = [(fact, src, line_no, vectorize(fact, aliases)) for fact, src, line_no in facts]
    for sentence in chunks:
        sv = vectorize(sentence, aliases)
        ranked = sorted(((cosine(sv, fv), fact, src, line_no) for fact, src, line_no, fv in fact_vectors), reverse=True, key=lambda x:x[0])
        score, fact, src, line_no = ranked[0] if ranked else (0, None, None, None)
        promotional = bool(re.search(r"\b(best|number one|#1|guaranteed|always|never|100%|world[- ]class|leading|fastest|revolutionary)\b", sentence, re.I))
        if fact and score >= 0.72:
            findings.append({"severity":"Info", "status":"SUPPORTED", "category":"Approved fact",
              "issue":f"Possible evidence match ({score:.0%} word/phrase cosine similarity). This is not proof of truth.",
              "evidence":f'Approved fact: {fact} · Source: {src["name"]} v{src["version"]} · line {line_no}',
              "source_id":src["id"], "source_version":src["version"], "match_score":round(score,4),
              "suggestion":"Check the complete context and confirm that the source is current and authoritative."})
        elif promotional:
            findings.append({"severity":"Medium", "status":"UNVERIFIED", "category":"Promotional claim",
              "issue":f"Strong promotional wording needs evidence: “{sentence[:180]}”",
              "evidence":"No sufficiently similar approved fact was found in the active source library.",
              "source_id":None, "source_version":None, "match_score":round(score,4),
              "suggestion":"Attach an approved source for this claim, or soften/remove the wording."})
        elif facts:
            findings.append({"severity":"Low", "status":"UNVERIFIED", "category":"Fact check",
              "issue":f"No close approved fact was found for: “{sentence[:180]}”",
              "evidence":"The active brand library does not contain a sufficiently similar approved fact.",
              "source_id":None, "source_version":None, "match_score":round(score,4),
              "suggestion":"Verify this statement with an authorised source before publishing."})
    if not findings:
        findings = [{"severity":"Info", "status":"UNVERIFIED", "category":"Evidence coverage",
          "issue":"No explicit configured terminology violation or strong claim was detected.",
          "evidence":"A clean automated scan is not proof that every statement is accurate.",
          "source_id":None,"source_version":None,"match_score":0.0,
          "suggestion":"Ask an authorised reviewer to verify important claims before publishing."}]
    unique=[]; seen=set()
    for f in findings:
        key=(f["status"],f["category"],f["issue"])
        if key not in seen: seen.add(key); unique.append(f)
    return unique


@app.get("/", response_class=HTMLResponse)
def home(): return (ROOT/"static/index.html").read_text(encoding="utf-8")

@app.get("/api/health")
def health():
    with connect() as c:
        counts = {"sources":c.execute("SELECT COUNT(*) FROM sources WHERE active=1").fetchone()[0],
                  "reviews":c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
                  "users":c.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]}
    return {"status":"ok","product":"BrandShield AI","version":"3.0.0","auth_enabled":AUTH_ENABLED,"counts":counts}

@app.get("/api/auth/config")
def auth_config(): return {"enabled":AUTH_ENABLED,"mode":"authenticated" if AUTH_ENABLED else "single-user-demo"}

class Login(BaseModel):
    email: str
    password: str

@app.post("/api/auth/login")
def login(data: Login, response: Response, request: Request):
    if not AUTH_ENABLED: return {"ok":True,"mode":"single-user-demo","user":{"email":"demo@brandshield.local","role":"admin"}}
    with connect() as c: row=c.execute("SELECT * FROM users WHERE email=? AND active=1",(data.email.strip().lower(),)).fetchone()
    if not row or not verify_password(data.password, row["password_hash"]): raise HTTPException(401,"Email or password is incorrect.")
    exp=int((datetime.now(timezone.utc)+timedelta(hours=SESSION_HOURS)).timestamp())
    token=sign_session({"sub":row["id"],"exp":exp})
    response.set_cookie(SESSION_COOKIE,token,httponly=True,secure=(request.url.scheme == "https"),samesite="lax",max_age=SESSION_HOURS*3600)
    return {"ok":True,"user":{"id":row["id"],"email":row["email"],"role":row["role"]}}

@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok":True}

@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return {"user":user,"auth_enabled":AUTH_ENABLED}

class UserCreate(BaseModel):
    email: str
    password: str = Field(min_length=12)
    role: str

@app.get("/api/users")
def get_users(user=Depends(require_roles("admin"))):
    with connect() as c: rows=c.execute("SELECT id,email,role,created,active FROM users ORDER BY created").fetchall()
    return [dict(x) for x in rows]

@app.post("/api/users")
def add_user(data: UserCreate, user=Depends(require_roles("admin"))):
    email=data.email.strip().lower()
    if data.role not in ("editor","reviewer"): raise HTTPException(400,"Role must be editor or reviewer.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",email): raise HTTPException(400,"Enter a valid email address.")
    try:
        with connect() as c: c.execute("INSERT INTO users VALUES(?,?,?,?,?,1)",(str(uuid.uuid4()),email,hash_password(data.password),data.role,stamp()))
    except sqlite3.IntegrityError: raise HTTPException(409,"A user with this email already exists.")
    return {"created":True,"email":email,"role":data.role}

@app.patch("/api/users/{uid}/deactivate")
def deactivate_user(uid: str, user=Depends(require_roles("admin"))):
    if uid==user["id"]: raise HTTPException(400,"You cannot deactivate your own account.")
    with connect() as c: cur=c.execute("UPDATE users SET active=0 WHERE id=?",(uid,))
    if not cur.rowcount: raise HTTPException(404,"User not found")
    return {"deactivated":True}

@app.get("/api/sources")
def get_sources(user=Depends(current_user)):
    rows=list_sources(True)
    return [{k:v for k,v in x.items() if k!="content"} | {"characters":len(x["content"])} for x in rows]

@app.get("/api/sources/versions")
def source_versions(user=Depends(current_user)):
    return [{k:v for k,v in x.items() if k!="content"} | {"characters":len(x["content"])} for x in list_sources(False)]

@app.post("/api/sources")
async def add_source(name: str=Form(...), content: Optional[str]=Form(None), file: Optional[UploadFile]=File(None), user=Depends(require_roles("admin","editor"))):
    if file and file.filename:
        body=extract(file.filename,await file.read()); name=file.filename; kind=Path(file.filename).suffix.lower()
    else: body=(content or "").strip(); kind="text"
    if not body: raise HTTPException(400,"Add source text or upload a supported file.")
    name=name.strip() or "Untitled source"
    with connect() as c:
        prev=c.execute("SELECT id,version FROM sources WHERE lower(name)=lower(?) ORDER BY version DESC LIMIT 1",(name,)).fetchone()
        version=(prev["version"]+1) if prev else 1
        sid=str(uuid.uuid4()); t=stamp()
        if prev: c.execute("UPDATE sources SET active=0 WHERE lower(name)=lower(?)",(name,))
        c.execute("INSERT INTO sources(id,name,kind,content,created,version,active,supersedes_id) VALUES(?,?,?,?,?,?,1,?)",(sid,name,kind,body,t,version,prev["id"] if prev else None))
    return {"id":sid,"name":name,"version":version,"supersedes":prev["id"] if prev else None}

@app.delete("/api/sources/{sid}")
def remove_source(sid: str, user=Depends(require_roles("admin","editor"))):
    with connect() as c: cur=c.execute("UPDATE sources SET active=0 WHERE id=? AND active=1",(sid,))
    if not cur.rowcount: raise HTTPException(404,"Active source not found")
    return {"archived":True}

class Review(BaseModel):
    title: str="Untitled content"
    content: str

@app.post("/api/reviews")
def create_review(data: Review, user=Depends(current_user)):
    if not data.content.strip(): raise HTTPException(400,"Content is required.")
    if len(data.content)>100_000: raise HTTPException(413,"Content is too long (maximum 100,000 characters).")
    rid=str(uuid.uuid4()); t=stamp(); findings=analyse(data.content)
    with connect() as c:
        c.execute("INSERT INTO reviews(id,title,content,findings,status,created,updated,created_by) VALUES(?,?,?,?,?,?,?,?)",(rid,data.title.strip() or "Untitled content",data.content,json.dumps(findings),"Pending",t,t,user["email"]))
        c.execute("INSERT INTO events(review_id,action,note,created,actor) VALUES(?,?,?,?,?)",(rid,"Review created","Automated evidence checks completed",t,user["email"]))
    return {"id":rid,"title":data.title,"status":"Pending","findings":findings,"created":t}

@app.get("/api/reviews")
def list_reviews(user=Depends(current_user)):
    with connect() as c: rows=c.execute("SELECT * FROM reviews ORDER BY created DESC LIMIT 500").fetchall()
    return [dict(id=r["id"],title=r["title"],status=r["status"],created=r["created"],created_by=r["created_by"],count=len(json.loads(r["findings"]))) for r in rows]

@app.get("/api/reviews/{rid}")
def get_review(rid: str,user=Depends(current_user)):
    with connect() as c:
        r=c.execute("SELECT * FROM reviews WHERE id=?",(rid,)).fetchone()
        ev=c.execute("SELECT action,note,created,actor FROM events WHERE review_id=? ORDER BY id DESC",(rid,)).fetchall()
    if not r: raise HTTPException(404,"Review not found")
    return dict(id=r["id"],title=r["title"],content=r["content"],status=r["status"],created_by=r["created_by"],findings=json.loads(r["findings"]),events=[dict(x) for x in ev])

class Decision(BaseModel):
    action: str
    note: str=""

@app.post("/api/reviews/{rid}/decision")
def decide(rid: str,data: Decision,user=Depends(require_roles("admin","reviewer"))):
    if data.action not in ("Approve","Request revision","Reject"): raise HTTPException(400,"Invalid action")
    t=stamp()
    with connect() as c:
        cur=c.execute("UPDATE reviews SET status=?,updated=? WHERE id=?",(data.action,t,rid))
        if not cur.rowcount: raise HTTPException(404,"Review not found")
        c.execute("INSERT INTO events(review_id,action,note,created,actor) VALUES(?,?,?,?,?)",(rid,data.action,data.note[:2000],t,user["email"]))
    return {"id":rid,"status":data.action}

@app.get("/api/analytics")
def analytics(user=Depends(require_roles("admin","reviewer"))):
    with connect() as c:
        reviews=c.execute("SELECT id,status,created,findings FROM reviews ORDER BY created DESC").fetchall()
        sources=c.execute("SELECT COUNT(*) FROM sources WHERE active=1").fetchone()[0]
        versions=c.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        users=c.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    statuses=Counter(r["status"] for r in reviews); categories=Counter(); finding_status=Counter()
    for r in reviews:
        for f in json.loads(r["findings"]): categories[f.get("category","Other")]+=1; finding_status[f.get("status","UNVERIFIED")]+=1
    daily=Counter(r["created"][:10] for r in reviews)
    return {"total_reviews":len(reviews),"active_sources":sources,"source_versions":versions,"active_users":users,
      "review_decisions":dict(statuses),"finding_status":dict(finding_status),"issue_categories":dict(categories),
      "daily_reviews":[{"date":k,"count":v} for k,v in sorted(daily.items())[-14:]],
      "note":"Descriptive counts from this workspace; they are not a claim of model accuracy."}

# Small, explicit, repeatable labelled set. Expected labels are reviewed examples, not production ground truth.
EVAL_SET = [
 {"text":"Our service calls all customers customers.","avoid":"customers","expected":True,"type":"avoid_term"},
 {"text":"Our platform is the best in the world.","avoid":"customers","expected":False,"type":"avoid_term"},
 {"text":"We guarantee 100% results.","avoid":"customers","expected":False,"type":"avoid_term"},
 {"text":"Members can access the dashboard.","avoid":"customers","expected":False,"type":"avoid_term"},
 {"text":"Our product is guaranteed to be the fastest.","expected":True,"type":"promotional_claim"},
 {"text":"The best platform, always guaranteed.","expected":True,"type":"promotional_claim"},
 {"text":"Our team publishes a monthly update.","expected":False,"type":"promotional_claim"},
 {"text":"The guide explains the setup process.","expected":False,"type":"promotional_claim"}
]

@app.get("/api/evaluation")
def evaluation(user=Depends(require_roles("admin","reviewer"))):
    metrics={}
    for kind in ("avoid_term","promotional_claim"):
        rows=[x for x in EVAL_SET if x["type"]==kind]; tp=fp=fn=tn=0
        for row in rows:
            if kind=="avoid_term": predicted=bool(re.search(r"(?<!\w)"+re.escape(row.get("avoid","customers"))+r"(?!\w)",row["text"],re.I))
            else: predicted=bool(re.search(r"\b(best|number one|#1|guaranteed|always|never|100%|world[- ]class|leading|fastest|revolutionary)\b",row["text"],re.I))
            expected=row["expected"]
            if predicted and expected: tp+=1
            elif predicted and not expected: fp+=1
            elif not predicted and expected: fn+=1
            else: tn+=1
        metrics[kind]={"labelled_examples":len(rows),"true_positive":tp,"false_positive":fp,"false_negative":fn,"true_negative":tn,
          "precision":round(tp/(tp+fp),3) if tp+fp else None,"recall":round(tp/(tp+fn),3) if tp+fn else None}
    return {"dataset":"BrandShield starter labelled checks v1","examples":len(EVAL_SET),"metrics":metrics,
      "disclaimer":"Small, hand-labelled smoke-evaluation set. Metrics describe only these examples and must not be presented as general model performance."}

@app.post("/api/demo/seed")
def seed(user=Depends(current_user)):
    with connect() as c:
        n=c.execute("SELECT COUNT(*) FROM sources WHERE active=1").fetchone()[0]
        if n==0:
            demo="Preferred term: members\nAvoid term: customers\nApproved fact: Our platform helps small businesses organise customer conversations in one workspace.\nApproved fact: The platform is available on web and mobile."
            c.execute("INSERT INTO sources(id,name,kind,content,created,version,active) VALUES(?,?,?,?,?,1,1)",(str(uuid.uuid4()),"Example Brand Guidelines","text",demo,stamp()))
    return {"ok":True}

@app.delete("/api/reviews")
def clear_reviews(request: Request,user=Depends(require_roles("admin"))):
    # Preserve sources and user accounts; remove review data and its event trail together.
    with connect() as c:
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM reviews")
    return {"cleared":True}

@app.get("/api/conflicts")
def conflicts(user=Depends(require_roles("admin","reviewer"))):
    _,_,facts,_=parse_rules(list_sources(True))
    return {"count":len(detect_conflicts(facts)),"items":detect_conflicts(facts),
            "note":"Automated polarity-overlap candidates require human confirmation."}
