import os, json, sqlite3, urllib.request, urllib.error
from datetime import date, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

def build_router(db, current_user):
    router = APIRouter()
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS study_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            exam_date TEXT NOT NULL, subjects TEXT NOT NULL, hours REAL NOT NULL,
            plan_json TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS doubt_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            question TEXT NOT NULL, answer TEXT NOT NULL, language TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")

    class PlannerRequest(BaseModel):
        exam_date: str
        subjects: list[str]
        daily_hours: float
        pending_topics: Optional[list[str]] = []

    class EvaluateRequest(BaseModel):
        question: str
        answer: str
        subject: Optional[str] = ""
        marks: int = 10
        language: str = "Hinglish"

    class FlashcardRequest(BaseModel):
        text: str
        count: int = 10
        language: str = "Hinglish"

    class TutorRequest(BaseModel):
        question: str
        branch: Optional[str] = ""
        semester: Optional[str] = ""
        subject: Optional[str] = ""
        language: str = "Hinglish"

    def ai(messages, temperature=0.3):
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise HTTPException(503, "AI service is not configured. Administrator must set OPENAI_API_KEY on Render.")
        payload = {"model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "messages": messages,
                   "temperature": temperature}
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                return json.loads(response.read())["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode()).get("error", {}).get("message", "")
            except Exception:
                detail = ""
            code = 503 if exc.code in (401, 429) else 502
            raise HTTPException(code, "AI provider error (HTTP " + str(exc.code) + "): " + detail[:220])
        except Exception as exc:
            raise HTTPException(502, "AI service connection failed: " + str(exc)[:160])

    def json_from_text(value):
        value = value.strip()
        if value.startswith("```"):
            value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        start, end = value.find("{"), value.rfind("}")
        if start >= 0 and end > start:
            value = value[start:end+1]
        return json.loads(value)

    @router.post("/api/ai/planner")
    def create_plan(data: PlannerRequest, user=Depends(current_user)):
        try:
            exam = date.fromisoformat(data.exam_date)
        except ValueError:
            raise HTTPException(400, "Exam date must be YYYY-MM-DD.")
        if exam < date.today(): raise HTTPException(400, "Exam date must be today or in the future.")
        if not data.subjects or not 0.5 <= data.daily_hours <= 16:
            raise HTTPException(400, "Add at least one subject and daily hours between 0.5 and 16.")
        prompt = {"exam_date": data.exam_date, "subjects": data.subjects,
                  "daily_hours": data.daily_hours, "pending_topics": data.pending_topics or [],
                  "days": (exam-date.today()).days + 1}
        try:
            raw = ai([
                {"role":"system","content":"Create a realistic student study timetable. Return ONLY JSON object with key days: array of objects {date, sessions:[{time,subject,topic,minutes}], goal}. Balance subjects, add revision and breaks. Do not invent syllabus; use only supplied subjects/topics. Keep daily study minutes within the available hours."},
                {"role":"user","content":json.dumps(prompt)}])
            plan = json_from_text(raw)
            if not isinstance(plan.get("days"), list): raise ValueError("Invalid plan")
            mode = "AI"
        except HTTPException:
            raise
        except Exception:
            total_days = max(1, (exam-date.today()).days + 1)
            topics = data.pending_topics or data.subjects
            days = []
            for i in range(total_days):
                topic = topics[i % len(topics)]
                minutes = int(data.daily_hours * 60)
                days.append({"date":(date.today()+timedelta(days=i)).isoformat(),
                    "sessions":[{"time":"Flexible study block","subject":data.subjects[i%len(data.subjects)],
                    "topic":topic,"minutes":minutes}], "goal":"Study and revise " + topic})
            plan, mode = {"days":days}, "basic"
        with db() as c:
            cur = c.execute("INSERT INTO study_plans(user_id,exam_date,subjects,hours,plan_json) VALUES(?,?,?,?,?)",
                (user["id"], data.exam_date, json.dumps(data.subjects), data.daily_hours, json.dumps(plan)))
            plan_id = cur.lastrowid
        return {"id":plan_id, "plan":plan, "mode":mode}

    @router.get("/api/ai/planner")
    def get_plan(user=Depends(current_user)):
        with db() as c:
            row = c.execute("SELECT id,exam_date,subjects,hours,plan_json,created_at FROM study_plans WHERE user_id=? ORDER BY id DESC LIMIT 1",(user["id"],)).fetchone()
        return dict(row) | {"subjects":json.loads(row["subjects"]), "plan":json.loads(row["plan_json"])} if row else {"plan":None}

    @router.post("/api/ai/evaluate")
    def evaluate(data: EvaluateRequest, user=Depends(current_user)):
        if not data.question.strip() or not data.answer.strip(): raise HTTPException(400,"Question and your answer are required.")
        if not 1 <= data.marks <= 100: raise HTTPException(400,"Maximum marks must be from 1 to 100.")
        result = ai([
            {"role":"system","content":"You are a fair RGPV engineering exam evaluator. Evaluate only the student's submitted answer against the question. Return ONLY JSON: {score:number,max_marks:number,what_was_good:[...],missing_points:[...],mistakes:[...],improved_answer:string,exam_tip:string}. Be constructive, don't claim official examiner marks. Use the requested language."},
            {"role":"user","content":json.dumps({"question":data.question,"student_answer":data.answer,"subject":data.subject,"max_marks":data.marks,"language":data.language})}])
        try: parsed=json_from_text(result)
        except Exception: raise HTTPException(502,"AI evaluator returned an unreadable response. Please try again.")
        return {"evaluation":parsed,"notice":"AI-generated practice feedback; actual university marks may differ."}

    @router.post("/api/ai/flashcards")
    def flashcards(data: FlashcardRequest, user=Depends(current_user)):
        if len(data.text.strip()) < 40: raise HTTPException(400,"Paste at least 40 characters of notes or syllabus text.")
        count=max(3,min(30,data.count))
        result=ai([
            {"role":"system","content":"Create concise study flashcards only from supplied notes. Return ONLY JSON object {flashcards:[{question,answer}]} in requested language. Do not add facts absent from notes."},
            {"role":"user","content":json.dumps({"notes":data.text[:18000],"count":count,"language":data.language})}])
        try: cards=json_from_text(result).get("flashcards",[])
        except Exception: raise HTTPException(502,"Could not read flashcards response. Please try again.")
        return {"flashcards":cards[:count],"count":len(cards[:count])}

    @router.post("/api/ai/tutor")
    def tutor(data: TutorRequest, user=Depends(current_user)):
        if not data.question.strip(): raise HTTPException(400,"Please enter your doubt.")
        answer=ai([
            {"role":"system","content":"You are AI Study Buddy, an RGPV engineering tutor. Explain accurately at student level, step by step, with a simple example and short exam-ready summary. Reply in the requested language (Hindi, Hinglish or English). Do not invent official syllabus details."},
            {"role":"user","content":json.dumps(data.dict())}])
        with db() as c:
            c.execute("INSERT INTO doubt_history(user_id,question,answer,language) VALUES(?,?,?,?)",
                (user["id"],data.question,answer,data.language))
        return {"answer":answer,"mode":"AI"}

    @router.get("/api/ai/doubts")
    def doubts(user=Depends(current_user)):
        with db() as c:
            rows=c.execute("SELECT id,question,answer,language,created_at FROM doubt_history WHERE user_id=? ORDER BY id DESC LIMIT 50",(user["id"],)).fetchall()
        return [dict(r) for r in rows]

    @router.post("/api/ai/pdf-chat")
    async def pdf_chat(question: str = Form(...), file: UploadFile = File(...), user=Depends(current_user)):
        if not question.strip(): raise HTTPException(400,"Enter a question about your PDF.")
        if not (file.filename or "").lower().endswith(".pdf"): raise HTTPException(400,"Please upload a PDF file.")
        raw=await file.read()
        if len(raw)>12*1024*1024: raise HTTPException(413,"PDF must be smaller than 12 MB.")
        try:
            from pypdf import PdfReader
            import io
            reader=PdfReader(io.BytesIO(raw))
            if len(reader.pages)>250: raise HTTPException(400,"PDF is limited to 250 pages.")
            pages=[]
            for index,page in enumerate(reader.pages):
                text=(page.extract_text() or "").strip()
                if text: pages.append({"page":index+1,"text":text[:7000]})
        except HTTPException: raise
        except ImportError: raise HTTPException(503,"PDF reader dependency is missing. Please redeploy after installing pypdf.")
        except Exception: raise HTTPException(400,"Could not read this PDF. It may be scanned or password protected.")
        if not pages: raise HTTPException(422,"No selectable text found. Scanned PDFs need OCR before they can be searched.")
        context="\n\n".join("[PAGE "+str(p["page"])+"]\n"+p["text"] for p in pages)
        answer=ai([
            {"role":"system","content":"Answer using only the supplied PDF text. Cite page numbers inline like (PDF p. 3). If the PDF does not contain the answer, say so. Do not invent citations."},
            {"role":"user","content":"Question: "+question+"\nPDF content:\n"+context[:50000]}])
        return {"answer":answer,"pages_used":[p["page"] for p in pages],"filename":file.filename}

    @router.get("/api/weak-topics")
    def weak_topics(user=Depends(current_user)):
        with db() as c:
            rows=c.execute("""SELECT q.branch,q.semester,q.subject,q.question,
                COUNT(*) attempts,SUM(i.is_correct) correct
                FROM quiz_attempt_items i JOIN quiz_attempts a ON a.id=i.attempt_id
                JOIN questions q ON q.id=i.question_id WHERE a.user_id=?
                GROUP BY i.question_id ORDER BY attempts DESC""",(user["id"],)).fetchall()
        result=[]
        for r in rows:
            attempts=r["attempts"] or 0; correct=r["correct"] or 0
            result.append({"branch":r["branch"],"semester":r["semester"],"subject":r["subject"],
                "topic":r["question"],"attempts":attempts,"accuracy":round(correct/attempts*100) if attempts else 0,
                "status":"Needs practice" if correct/attempts < .6 else "Good"})
        result.sort(key=lambda x:x["accuracy"])
        return {"topics":result,"message":"Take subject-tagged quizzes to build a more detailed weak-topic map." if not result else "Topics are ranked by your quiz accuracy."}

    return router
