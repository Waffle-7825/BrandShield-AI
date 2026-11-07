from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone
import sqlite3, uuid, json, re, io

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB = DATA / "brandshield.sqlite3"
app = FastAPI(title="BrandShield AI")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

def connect():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

with connect() as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,name TEXT,kind TEXT,content TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,title TEXT,content TEXT,findings TEXT,status TEXT,created TEXT,updated TEXT);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,review_id TEXT,action TEXT,note TEXT,created TEXT);
    """)

def extract(filename, raw):
    ext = Path(filename).suffix.lower()
    if ext in (".txt", ".md"): return raw.decode("utf-8", errors="replace")
    if ext == ".pdf":
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        return "\n".join(f"[Page {i+1}] {p.get_text()}" for i,p in enumerate(doc))
    if ext == ".docx":
        from docx import Document
        d = Document(io.BytesIO(raw))
        return "\n".join(p.text for p in d.paragraphs)
    raise HTTPException(400, "Supported files: PDF, DOCX, TXT, MD")

def sources():
    with connect() as c:
        return [dict(x) for x in c.execute("SELECT * FROM sources ORDER BY created DESC")]

def words(s):
    return set(re.findall(r"[a-z0-9']+", s.lower()))

def analyse(text):
    refs = sources()
    preferred, avoid, facts = [], [], []
    for src in refs:
        for line in src["content"].splitlines():
            line=line.strip()
            for prefix, bucket in [("preferred term:",preferred),("avoid term:",avoid),("approved fact:",facts)]:
                if line.lower().startswith(prefix):
                    bucket.append((line.split(":",1)[1].strip(),src))
    findings=[]
    low=text.lower()
    for term,src in avoid:
        if term and re.search(r"\b"+re.escape(term.lower())+r"\b",low):
            findings.append(dict(severity="High",status="CONTRADICTED",category="Terminology",
                issue=f'The content uses “{term}”, which the brand reference says to avoid.',
                evidence=f'{src["name"]} · Avoid term: {term}',
                suggestion="Replace it with the approved preferred term, if one is defined."))
    chunks=[x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+",text) if len(x.strip())>=18]
    for sentence in chunks:
        st=words(sentence); best=None; score=0
        for fact,src in facts:
            fw=words(fact)
            candidate=len(st & fw)/max(1,len(fw))
            if candidate>score: score=candidate; best=(fact,src)
        if best and score>=.72:
            findings.append(dict(severity="Info",status="SUPPORTED",category="Approved fact",
                issue=f"Possible match to an approved fact ({score:.0%} word overlap).",
                evidence=f'Approved fact: {best[0]} · Source: {best[1]["name"]}',
                suggestion="Check that the full context and wording remain accurate."))
        elif re.search(r"\b(best|number one|#1|guaranteed|always|never|100%|world[- ]class|leading|fastest)\b",sentence,re.I):
            findings.append(dict(severity="Medium",status="UNVERIFIED",category="Promotional claim",
                issue=f"Strong promotional wording needs evidence: “{sentence[:180]}”",
                evidence="No sufficiently matching approved fact was found in the current library.",
                suggestion="Attach an approved source for this claim, or soften/remove the wording."))
        elif facts:
            findings.append(dict(severity="Low",status="UNVERIFIED",category="Fact check",
                issue=f"No close approved fact was found for: “{sentence[:180]}”",
                evidence="The current brand library does not contain a sufficiently matching fact.",
                suggestion="Verify this statement with an authorised source before publishing."))
    if not findings:
        findings=[dict(severity="Info",status="UNVERIFIED",category="Evidence coverage",
            issue="No explicit configured terminology violation or strong claim was detected.",
            evidence="A clean automated scan is not proof that every statement is accurate.",
            suggestion="Ask an authorised reviewer to verify important claims before publishing.")]
    out=[]; seen=set()
    for f in findings:
        k=(f["status"],f["category"],f["issue"])
        if k not in seen: seen.add(k); out.append(f)
    return out

@app.get("/",response_class=HTMLResponse)
def home(): return (ROOT/"static/index.html").read_text(encoding="utf-8")

@app.get("/api/health")
def health(): return {"status":"ok","product":"BrandShield AI"}

@app.get("/api/sources")
def list_sources(): return sources()

@app.post("/api/sources")
async def add_source(name: str=Form(...), content: Optional[str]=Form(None), file: Optional[UploadFile]=File(None)):
    if file and file.filename:
        body=extract(file.filename,await file.read()); name=file.filename; kind=Path(file.filename).suffix.lower()
    else:
        body=(content or "").strip(); kind="text"
    if not body: raise HTTPException(400,"Add source text or upload a supported file.")
    sid=str(uuid.uuid4())
    with connect() as c: c.execute("INSERT INTO sources VALUES(?,?,?,?,?)",(sid,name,kind,body,stamp()))
    return {"id":sid,"name":name}

@app.delete("/api/sources/{sid}")
def remove_source(sid):
    with connect() as c: cur=c.execute("DELETE FROM sources WHERE id=?",(sid,))
    if not cur.rowcount: raise HTTPException(404,"Source not found")
    return {"deleted":True}

class Review(BaseModel):
    title: str="Untitled content"
    content: str

@app.post("/api/reviews")
def create_review(data: Review):
    if not data.content.strip(): raise HTTPException(400,"Content is required.")
    rid=str(uuid.uuid4()); t=stamp(); findings=analyse(data.content)
    with connect() as c:
        c.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?)",(rid,data.title,data.content,json.dumps(findings),"Pending",t,t))
        c.execute("INSERT INTO events(review_id,action,note,created) VALUES(?,?,?,?)",(rid,"Review created","Automated checks completed",t))
    return {"id":rid,"title":data.title,"status":"Pending","findings":findings,"created":t}

@app.get("/api/reviews")
def list_reviews():
    with connect() as c: rows=c.execute("SELECT * FROM reviews ORDER BY created DESC").fetchall()
    return [dict(id=r["id"],title=r["title"],status=r["status"],created=r["created"],count=len(json.loads(r["findings"]))) for r in rows]

@app.get("/api/reviews/{rid}")
def get_review(rid):
    with connect() as c:
        r=c.execute("SELECT * FROM reviews WHERE id=?",(rid,)).fetchone()
        ev=c.execute("SELECT action,note,created FROM events WHERE review_id=? ORDER BY id DESC",(rid,)).fetchall()
    if not r: raise HTTPException(404,"Review not found")
    return dict(id=r["id"],title=r["title"],content=r["content"],status=r["status"],findings=json.loads(r["findings"]),events=[dict(x) for x in ev])

class Decision(BaseModel):
    action: str
    note: str=""

@app.post("/api/reviews/{rid}/decision")
def decide(rid, data: Decision):
    if data.action not in ("Approve","Request revision","Reject"): raise HTTPException(400,"Invalid action")
    t=stamp()
    with connect() as c:
        cur=c.execute("UPDATE reviews SET status=?,updated=? WHERE id=?",(data.action,t,rid))
        if not cur.rowcount: raise HTTPException(404,"Review not found")
        c.execute("INSERT INTO events(review_id,action,note,created) VALUES(?,?,?,?)",(rid,data.action,data.note,t))
    return {"id":rid,"status":data.action}

@app.post("/api/demo/seed")
def seed():
    with connect() as c:
        n=c.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        if n==0:
            demo="Preferred term: members\nAvoid term: customers\nApproved fact: Our platform helps small businesses organise customer conversations in one workspace.\nApproved fact: The platform is available on web and mobile."
            c.execute("INSERT INTO sources VALUES(?,?,?,?,?)",(str(uuid.uuid4()),"Example Brand Guidelines","text",demo,stamp()))
    return {"ok":True}
