from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from typing import Optional, List
import anthropic, os, uuid, json
from datetime import datetime, timedelta
import jwt, sqlite3, hashlib

app = FastAPI(title="SKYnet API")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
JWT_SECRET    = os.getenv("JWT_SECRET", "skynet-secret-change-me")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

print(f"STARTUP: ANTHROPIC_KEY présent = {bool(ANTHROPIC_KEY)}, longueur = {len(ANTHROPIC_KEY)}", flush=True)

# ── BASE DE DONNÉES ────────────────────────────────────────
def get_db():
    conn = sqlite3.connect("skynet.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, name TEXT, email TEXT UNIQUE,
        password_hash TEXT, plan TEXT DEFAULT 'starter',
        clips_used INTEGER DEFAULT 0, clips_limit INTEGER DEFAULT 5,
        created_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS videos (
        id TEXT PRIMARY KEY, user_id TEXT, url TEXT,
        status TEXT DEFAULT 'processing', clips_json TEXT,
        created_at TEXT)""")
    db.commit()
    db.close()

init_db()

# ── UTILITAIRES ────────────────────────────────────────────
def hash_password(p): return hashlib.sha256(p.encode()).hexdigest()
def make_token(user_id):
    return jwt.encode({"sub": user_id, "exp": datetime.utcnow() + timedelta(days=30)}, JWT_SECRET, algorithm="HS256")

def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id=?", (payload["sub"],)).fetchone()
        db.close()
        if not user: raise HTTPException(status_code=401, detail="Utilisateur introuvable")
        return dict(user)
    except:
        raise HTTPException(status_code=401, detail="Token invalide")

# ── AUTH ───────────────────────────────────────────────────
class RegisterBody(BaseModel):
    name: str
    email: str
    password: str

@app.post("/api/auth/register")
def register(body: RegisterBody):
    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE email=?", (body.email,)).fetchone()
    if existing:
        db.close()
        raise HTTPException(status_code=400, detail="Email déjà utilisé")
    user_id = str(uuid.uuid4())
    db.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?)",
        (user_id, body.name, body.email, hash_password(body.password),
         "starter", 0, 5, datetime.utcnow().isoformat()))
    db.commit()
    db.close()
    user = {"id": user_id, "name": body.name, "email": body.email,
            "plan": "starter", "clips_used": 0, "clips_limit": 5}
    return {"access_token": make_token(user_id), "user": user}

@app.post("/api/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends()):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=? AND password_hash=?",
        (form.username, hash_password(form.password))).fetchone()
    db.close()
    if not user: raise HTTPException(status_code=401, detail="Identifiants incorrects")
    user = dict(user)
    return {"access_token": make_token(user["id"]), "user": {
        "id": user["id"], "name": user["name"], "email": user["email"],
        "plan": user["plan"], "clips_used": user["clips_used"], "clips_limit": user["clips_limit"]}}

# ── VIDEOS ────────────────────────────────────────────────
class AnalyzeBody(BaseModel):
    url: str
    niche: str = "Tech & IA"
    nb_clips: int = 5
    target_duration: int = 45
    platforms: List[str] = ["tiktok", "instagram", "youtube"]
    language: str = "fr"

@app.post("/api/videos/analyze")
def analyze_video(body: AnalyzeBody, user=Depends(get_current_user)):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    if dict(u)["clips_used"] >= dict(u)["clips_limit"]:
        db.close()
        raise HTTPException(status_code=403, detail="Limite de clips atteinte")
    video_id = str(uuid.uuid4())
    db.execute("INSERT INTO videos VALUES (?,?,?,?,?,?)",
        (video_id, user["id"], body.url, "processing", None, datetime.utcnow().isoformat()))
    db.commit()
    db.close()
    import threading
    threading.Thread(target=run_analysis, args=(video_id, body, user["id"])).start()
    return {"video_id": video_id, "status": "processing"}

def run_analysis(video_id, body, user_id):
    try:
        print(f"ANALYSE START: video_id={video_id}, url={body.url}", flush=True)
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        prompt = f"""Tu es SKYnet, expert en contenu viral.
Niche: {body.niche}
URL/Contenu: {body.url}
Durée clips: {body.target_duration}s
Plateformes: {', '.join(body.platforms)}

Génère exactement {body.nb_clips} clips viraux en JSON pur (sans texte autour):
{{"clips":[{{"rank":1,"title":"...","hook_type":"Révélation","ts_start":"03:42","ts_end":"04:28","viral_score":94,"viral_tier":"high","retention_score":91,"description":"...","caption":"... #hashtag","hashtags":"#tag1 #tag2 #tag3","platforms":{body.platforms}}}]}}"""

        print(f"ANALYSE: Appel Claude API...", flush=True)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        print(f"ANALYSE: Réponse Claude reçue", flush=True)
        raw = message.content[0].text
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(cleaned)
        clips = parsed.get("clips", [])
        print(f"ANALYSE SUCCESS: {len(clips)} clips générés", flush=True)

        db = get_db()
        db.execute("UPDATE videos SET status='analyzed', clips_json=? WHERE id=?",
            (json.dumps(clips), video_id))
        db.execute("UPDATE users SET clips_used=clips_used+? WHERE id=?",
            (len(clips), user_id))
        db.commit()
        db.close()
    except Exception as e:
        print(f"ANALYSE ERROR: {type(e).__name__}: {e}", flush=True)
        db = get_db()
        db.execute("UPDATE videos SET status='failed' WHERE id=?", (video_id,))
        db.commit()
        db.close()

@app.get("/api/videos/{video_id}/status")
def video_status(video_id: str, user=Depends(get_current_user)):
    db = get_db()
    video = db.execute("SELECT * FROM videos WHERE id=? AND user_id=?",
        (video_id, user["id"])).fetchone()
    db.close()
    if not video: raise HTTPException(status_code=404, detail="Vidéo introuvable")
    video = dict(video)
    clips = json.loads(video["clips_json"]) if video["clips_json"] else []
    return {"status": video["status"], "clips": clips}

# ── ANALYTICS ─────────────────────────────────────────────
@app.get("/api/analytics/overview")
def analytics(user=Depends(get_current_user)):
    db = get_db()
    u = dict(db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone())
    total_videos = db.execute("SELECT COUNT(*) FROM videos WHERE user_id=?", (user["id"],)).fetchone()[0]
    db.close()
    return {"clips_generated": u["clips_used"], "total_views": u["clips_used"] * 45000,
            "subscribers_gained": u["clips_used"] * 159, "engagement_rate": 6.8}

@app.get("/")
def root():
    return {"status": "SKYnet API en ligne", "version": "1.0"}
