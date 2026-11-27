import os, sqlite3, hashlib, secrets, pathlib, re
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Header, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr

BASE = pathlib.Path(__file__).parent
DB = BASE / "studybuddy.db"
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)

app = FastAPI(title="AI Study Buddy", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT DEFAULT 'student', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS resources(id INTEGER PRIMARY KEY, title TEXT NOT NULL, branch TEXT, semester TEXT, subject TEXT, kind TEXT, filename TEXT NOT NULL, uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS questions(id INTEGER PRIMARY KEY, question TEXT, options TEXT, answer TEXT, explanation TEXT, branch TEXT, semester TEXT, subject TEXT);
        CREATE TABLE IF NOT EXISTS quiz_attempts(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, score INTEGER NOT NULL, total INTEGER NOT NULL, attempted_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS quiz_attempt_items(id INTEGER PRIMARY KEY, attempt_id INTEGER NOT NULL, question_id INTEGER NOT NULL, is_correct INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS resource_progress(user_id INTEGER NOT NULL, resource_id INTEGER NOT NULL, completed_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id,resource_id));
        """)
        count = c.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        if count == 0:
            sample = [
                ("What does CPU stand for?","Central Processing Unit|Computer Personal Unit|Central Program Utility|Control Processing User","Central Processing Unit","CPU is the main processor of a computer."),
                ("Which data structure uses FIFO?","Stack|Queue|Tree|Graph","Queue","Queue follows First In, First Out."),
                ("Which language is commonly used for AI/ML?","HTML|Python|CSS|SQL","Python","Python has widely used ML libraries such as scikit-learn and PyTorch."),
                ("What is the time complexity of binary search?","O(n)|O(n²)|O(log n)|O(1)","O(log n)","Binary search halves the search space at each step."),
                ("Which SQL command retrieves data?","GET|OPEN|SELECT|FETCHALL","SELECT","SELECT is used to query rows from a database.")
            ]
            c.executemany("INSERT INTO questions(question,options,answer,explanation) VALUES(?,?,?,?)", sample)
init_db()

class Register(BaseModel):
    name: str
    email: EmailStr
    password: str
class Login(BaseModel):
    email: EmailStr
    password: str
class Ask(BaseModel):
    question: str
    branch: Optional[str] = ""
    semester: Optional[str] = ""
    subject: Optional[str] = ""

def password_hash(p: str, salt: Optional[str] = None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", p.encode(), bytes.fromhex(salt), 180000).hex()
    return salt + "$" + digest

def verify(p, stored):
    try:
        salt, digest = stored.split("$", 1)
        return secrets.compare_digest(password_hash(p, salt), stored)
    except Exception:
        return False

def current_user(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Please log in")
    token = authorization[7:]
    with db() as c:
        row = c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>?", (token, datetime.utcnow().isoformat())).fetchone()
    if not row: raise HTTPException(401, "Session expired. Please log in again.")
    return dict(row)

@app.get("/")
def home():
    return FileResponse(BASE / "static" / "index.html")

@app.get("/api/health")
def health():
    return {"status":"ok","app":"AI Study Buddy"}

@app.post("/api/register")
def register(data: Register):
    if len(data.password) < 8: raise HTTPException(400, "Password must be at least 8 characters")
    with db() as c:
        try:
            role = "admin" if os.getenv("ADMIN_EMAIL", "").lower() == data.email.lower() and os.getenv("ADMIN_EMAIL") else "student"
            c.execute("INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)", (data.name.strip(), data.email.lower(), password_hash(data.password), role))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "This email is already registered")
    return {"message":"Account created. Please log in."}

@app.post("/api/login")
def login(data: Login):
    with db() as c:
        user = c.execute("SELECT * FROM users WHERE email=?", (data.email.lower(),)).fetchone()
        if not user or not verify(data.password, user["password"]): raise HTTPException(401, "Invalid email or password")
        admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
        if admin_email and user["email"].lower() == admin_email and user["role"] != "admin":
            c.execute("UPDATE users SET role='admin' WHERE id=?", (user["id"],))
            user = c.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        token = secrets.token_urlsafe(32)
        c.execute("INSERT INTO sessions(token,user_id,expires) VALUES(?,?,?)", (token,user["id"],(datetime.utcnow()+timedelta(days=7)).isoformat()))
    return {"token":token,"name":user["name"],"role":user["role"]}

@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"name":user["name"],"email":user["email"],"role":user["role"]}

@app.post("/api/logout")
def logout(authorization: Optional[str] = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        with db() as c: c.execute("DELETE FROM sessions WHERE token=?", (authorization[7:],))
    return {"message":"Logged out"}

@app.get("/api/resources")
def resources(branch: str="", semester: str="", subject: str=""):
    sql = "SELECT id,title,branch,semester,subject,kind,filename,uploaded_at FROM resources WHERE 1=1"
    args=[]
    for col,val in [("branch",branch),("semester",semester),("subject",subject)]:
        if val: sql += f" AND {col}=?"; args.append(val)
    with db() as c: rows=c.execute(sql+" ORDER BY id DESC",args).fetchall()
    return [dict(x) for x in rows]

@app.post("/api/resources")
async def upload_resource(title: str=Form(...), branch: str=Form(""), semester: str=Form(""), subject: str=Form(""), kind: str=Form("Notes"), file: UploadFile=File(...), user=Depends(current_user)):
    if user["role"] != "admin": raise HTTPException(403,"Admin access required")
    ext=pathlib.Path(file.filename or "").suffix.lower()
    if ext not in [".pdf",".txt",".doc",".docx"]: raise HTTPException(400,"Only PDF, TXT, DOC and DOCX files are allowed")
    safe_name=secrets.token_hex(8)+ext
    target=UPLOADS/safe_name
    content=await file.read()
    if len(content)>15*1024*1024: raise HTTPException(413,"File must be smaller than 15 MB")
    target.write_bytes(content)
    with db() as c:
        cur=c.execute("INSERT INTO resources(title,branch,semester,subject,kind,filename) VALUES(?,?,?,?,?,?)",(title,branch,semester,subject,kind,safe_name))
        rid=cur.lastrowid
    return {"id":rid,"message":"Resource uploaded"}

@app.get("/api/resources/{rid}/download")
def download_resource(rid: int):
    with db() as c: row=c.execute("SELECT * FROM resources WHERE id=?",(rid,)).fetchone()
    if not row: raise HTTPException(404,"Resource not found")
    path=UPLOADS/row["filename"]
    if not path.exists(): raise HTTPException(404,"File is unavailable")
    return FileResponse(path, filename=path.name)

@app.get("/api/quiz")
def quiz(branch: str="", semester: str="", subject: str=""):
    with db() as c: rows=c.execute("SELECT id,question,options,explanation FROM questions ORDER BY RANDOM() LIMIT 5").fetchall()
    return [{"id":r["id"],"question":r["question"],"options":r["options"].split("|")} for r in rows]

class QuizSubmit(BaseModel):
    answers: dict
@app.post("/api/quiz/submit")
def submit_quiz(data: QuizSubmit, user=Depends(current_user)):
    ids=[int(x) for x in data.answers.keys()]
    if not ids: return {"score":0,"total":0,"details":[]}
    marks=0; details=[]
    with db() as c:
        for qid in ids:
            q=c.execute("SELECT question,answer,explanation FROM questions WHERE id=?",(qid,)).fetchone()
            if q:
                correct=data.answers[str(qid)]==q["answer"]
                marks += int(correct)
                details.append({"question":q["question"],"correct":q["answer"],"explanation":q["explanation"],"is_correct":correct})
    with db() as c:
        cur=c.execute("INSERT INTO quiz_attempts(user_id,score,total) VALUES(?,?,?)",(user["id"],marks,len(details)))
        attempt_id=cur.lastrowid
        for qid in ids:
            q=c.execute("SELECT answer FROM questions WHERE id=?",(qid,)).fetchone()
            if q:
                c.execute("INSERT INTO quiz_attempt_items(attempt_id,question_id,is_correct) VALUES(?,?,?)",(attempt_id,qid,int(data.answers[str(qid)]==q["answer"])))
    return {"score":marks,"total":len(details),"details":details,"attempt_id":attempt_id}

@app.get("/api/progress")
def progress(user=Depends(current_user)):
    with db() as c:
        attempts=c.execute("SELECT COUNT(*) n, COALESCE(SUM(score),0) score, COALESCE(SUM(total),0) total FROM quiz_attempts WHERE user_id=?",(user["id"],)).fetchone()
        recent=c.execute("SELECT score,total,attempted_at FROM quiz_attempts WHERE user_id=? ORDER BY id DESC LIMIT 10",(user["id"],)).fetchall()
        completed=c.execute("SELECT COUNT(*) FROM resource_progress WHERE user_id=?",(user["id"],)).fetchone()[0]
        resources=c.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
    accuracy=round((attempts["score"]/attempts["total"])*100) if attempts["total"] else 0
    return {"quiz_attempts":attempts["n"],"correct_answers":attempts["score"],"questions_attempted":attempts["total"],"accuracy":accuracy,"resources_completed":completed,"resources_available":resources,"recent":[dict(x) for x in recent]}

@app.post("/api/resources/{rid}/complete")
def mark_resource_complete(rid:int,user=Depends(current_user)):
    with db() as c:
        exists=c.execute("SELECT id FROM resources WHERE id=?",(rid,)).fetchone()
        if not exists: raise HTTPException(404,"Resource not found")
        c.execute("INSERT OR IGNORE INTO resource_progress(user_id,resource_id) VALUES(?,?)",(user["id"],rid))
    return {"message":"Resource marked as completed"}

@app.post("/api/ask")
def ask(data: Ask, user=Depends(current_user)):
    q=data.question.strip()
    if not q: raise HTTPException(400,"Please enter a question")
    key_terms=[w for w in re.findall(r"[a-zA-Z0-9]{3,}",q.lower()) if w not in {"what","which","when","where","explain","about","with","from","this","that"}]
    snippets=[]
    for p in UPLOADS.glob("*.txt"):
        try:
            text=p.read_text(errors="ignore")
            for para in text.splitlines():
                if para.strip() and any(k in para.lower() for k in key_terms): snippets.append(para.strip()[:700])
        except Exception: pass
    api_key=os.getenv("OPENAI_API_KEY")
    if api_key:
        try:
            import urllib.request, json
            context="\n".join(snippets[:6])
            payload={"model":os.getenv("OPENAI_MODEL","gpt-4o-mini"),"messages":[{"role":"system","content":"You are AI Study Buddy, a friendly tutor for RGPV engineering students. Explain clearly in simple language with examples. If useful, answer in Hinglish. Use supplied study context when relevant. Do not invent official syllabus facts."},{"role":"user","content":f"Branch: {data.branch}; Semester: {data.semester}; Subject: {data.subject}\nStudy context: {context}\nQuestion: {q}"}],"temperature":0.4}
            req=urllib.request.Request("https://api.openai.com/v1/chat/completions",data=json.dumps(payload).encode(),headers={"Authorization":"Bearer "+api_key,"Content-Type":"application/json"})
            with urllib.request.urlopen(req,timeout=35) as res: answer=json.loads(res.read())["choices"][0]["message"]["content"]
            return {"answer":answer,"mode":"AI"}
        except urllib.error.HTTPError as exc:
            try:
                err_body=json.loads(exc.read().decode("utf-8"))
                detail=err_body.get("error",{}).get("message","")
            except Exception:
                detail=""
            if exc.code == 401:
                message="OpenAI rejected the API key. Check that OPENAI_API_KEY is copied correctly in Render."
            elif exc.code == 429:
                message="OpenAI API quota or billing limit reached. Add API billing/credits in the OpenAI Platform."
            elif exc.code == 404:
                message="The configured OpenAI model was not found or is not available to this API project."
            else:
                message="OpenAI request failed (HTTP "+str(exc.code)+"). "+detail[:180]
            raise HTTPException(502, message)
        except Exception as exc:
            raise HTTPException(502, "AI service connection error: "+str(exc)[:180])
    if snippets:
        return {"answer":"I found these related lines in the uploaded text resources:\n\n"+"\n".join("• "+s for s in snippets[:5])+"\n\nFor a detailed AI-generated explanation, an AI API key needs to be configured by the site administrator.","mode":"study-material"}
    return {"answer":"I can help with this topic. Please add your branch, semester and subject, and upload relevant notes/PDFs so I can ground the answer in your study material. For full AI-generated explanations, the administrator needs to configure an AI API key.","mode":"basic"}

@app.get("/api/admin/stats")
def stats(user=Depends(current_user)):
    if user["role"]!="admin": raise HTTPException(403,"Admin access required")
    with db() as c:
        return {"students":c.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0],"resources":c.execute("SELECT COUNT(*) FROM resources").fetchone()[0],"questions":c.execute("SELECT COUNT(*) FROM questions").fetchone()[0]}

@app.get("/api/admin/students")
def admin_students(user=Depends(current_user)):
    if user["role"] != "admin": raise HTTPException(403, "Admin access required")
    with db() as c:
        rows = c.execute("""
            SELECT u.id,u.name,u.email,u.created_at,
                   (SELECT COUNT(*) FROM quiz_attempts qa WHERE qa.user_id=u.id) AS quiz_attempts,
                   (SELECT COALESCE(SUM(qa.score),0) FROM quiz_attempts qa WHERE qa.user_id=u.id) AS correct_answers,
                   (SELECT COALESCE(SUM(qa.total),0) FROM quiz_attempts qa WHERE qa.user_id=u.id) AS questions_attempted,
                   (SELECT COUNT(*) FROM resource_progress rp WHERE rp.user_id=u.id) AS resources_completed
            FROM users u
            WHERE u.role='student'
            ORDER BY u.created_at DESC
        """).fetchall()
    return [dict(r) for r in rows]

# RGPV academic content catalogue (seeded, version-controlled JSON)
CATALOG_FILE = BASE / "data" / "academic_catalog.json"

@app.get("/api/academic/catalog")
def academic_catalog():
    import json
    if not CATALOG_FILE.exists():
        raise HTTPException(503, "Academic catalogue is not installed.")
    try:
        return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(500, "Academic catalogue could not be loaded.")

@app.get("/api/academic/subjects")
def academic_subjects(branch: str = "AIML", semester: int = 6):
    import json
    catalog = academic_catalog()
    for item in catalog.get("semesters", []):
        if item.get("branch", "").lower() == branch.lower() and item.get("semester") == semester:
            return {"branch": branch, "semester": semester, "scheme_note": item.get("scheme_note", ""), "subjects": item.get("subjects", [])}
    return {"branch": branch, "semester": semester, "scheme_note": "Content is being verified and added.", "subjects": []}

@app.get("/api/academic/search")
def academic_search(q: str = "", branch: str = "AIML", semester: int = 6):
    query = q.strip().lower()
    catalog = academic_catalog()
    result = academic_subjects(branch, semester)
    matches = []
    for subject in result["subjects"]:
        for unit in subject.get("units", []):
            for question in unit.get("important_questions", []):
                haystack = " ".join([subject.get("name", ""), unit.get("title", ""), question.get("question", ""), question.get("answer", "")]).lower()
                if not query or query in haystack:
                    matches.append({"subject_code": subject.get("code"), "subject": subject.get("name"), "unit": unit.get("unit"), "unit_title": unit.get("title"), **question})
    return {"count": len(matches), "questions": matches[:100], "label": catalog.get("question_label", "Practice questions")}



# Authenticated AI learning tools
from ai_features import build_router
app.include_router(build_router(db, current_user))
