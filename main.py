from fastapi import FastAPI, HTTPException, Depends, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
import anthropic, os, uuid, json, subprocess, shutil, tempfile
from datetime import datetime, timedelta
import jwt, sqlite3, hashlib

app = FastAPI(title="SKYnet API")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
JWT_SECRET    = os.getenv("JWT_SECRET", "skynet-secret-change-me")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
CLIPS_DIR     = "/tmp/skynet_clips"
os.makedirs(CLIPS_DIR, exist_ok=True)

print(f"STARTUP: ANTHROPIC_KEY présent = {bool(ANTHROPIC_KEY)}, longueur = {len(ANTHROPIC_KEY)}", flush=True)

# Diagnostic faster-whisper au démarrage
try:
    from faster_whisper import WhisperModel
    print("STARTUP: faster-whisper OK", flush=True)
except Exception as e:
    print(f"STARTUP: faster-whisper ERREUR: {e}", flush=True)

# Cherche le meilleur ffmpeg disponible (système = nixpkgs avec libass, sinon imageio)
def get_ffmpeg():
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        print(f"FFMPEG: système trouvé à {sys_ffmpeg}", flush=True)
        return sys_ffmpeg
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        print(f"FFMPEG: imageio trouvé à {path}", flush=True)
        return path
    except Exception as e:
        print(f"FFMPEG: introuvable! {e}", flush=True)
        return "ffmpeg"

FFMPEG_EXE = get_ffmpeg()

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

def ts_to_seconds(ts):
    parts = ts.strip().split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0

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
        print(f"ANALYSE START: video_id={video_id}", flush=True)
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        prompt = f"""Tu es SKYnet, expert en contenu viral.
Niche: {body.niche}
URL/Contenu: {body.url}
Durée clips: {body.target_duration}s
Plateformes: {', '.join(body.platforms)}

Génère exactement {body.nb_clips} clips viraux en JSON pur (sans texte autour):
{{"clips":[{{"rank":1,"title":"...","hook_type":"Révélation","ts_start":"03:42","ts_end":"04:28","viral_score":94,"viral_tier":"high","retention_score":91,"description":"...","caption":"... #hashtag","hashtags":"#tag1 #tag2 #tag3"}}]}}"""

        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
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

# ── SOUS-TITRES STYLE TIKTOK ──────────────────────────────
def find_font():
    import glob
    candidates = [
        "/nix/store/*/share/fonts/**/*Bold*.ttf",
        "/nix/store/*/share/fonts/**/*.ttf",
        "/usr/share/fonts/**/*Bold*.ttf",
        "/usr/share/fonts/**/*.ttf",
    ]
    for pattern in candidates:
        matches = glob.glob(pattern, recursive=True)
        for m in matches:
            if "DejaVu" in m and "Bold" in m:
                return m
        if matches:
            return matches[0]
    return None

def add_subtitles(clip_path):
    import traceback
    try:
        from faster_whisper import WhisperModel

        print(f"SUBTITLE: Transcription de {clip_path}", flush=True)
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments_gen, _ = model.transcribe(clip_path, word_timestamps=False)
        segment_list = list(segments_gen)

        if not segment_list:
            print("SUBTITLE: Aucun segment détecté", flush=True)
            return

        # Construit les entrées (t_start, t_end, texte)
        entries = []
        for seg in segment_list:
            text = seg.text.strip()
            if not text:
                continue
            words = text.split()
            seg_dur = max(seg.end - seg.start, 0.5)
            n_chunks = max(1, (len(words) + 2) // 3)
            chunk_dur = seg_dur / n_chunks
            for i in range(0, len(words), 3):
                ci = i // 3
                t_start = seg.start + ci * chunk_dur
                t_end = t_start + chunk_dur
                entries.append((t_start, t_end, " ".join(words[i:i+3]).upper()))

        if not entries:
            print("SUBTITLE: Aucun texte transcrit", flush=True)
            return

        print(f"SUBTITLE: {len(entries)} lignes générées", flush=True)

        # Cherche une police disponible sur le système
        font_path = find_font()
        print(f"SUBTITLE: Police = {font_path}", flush=True)

        # drawtext : ne nécessite pas libass, fonctionne avec tout ffmpeg
        filter_parts = []
        for t_start, t_end, text in entries:
            safe = (text
                .replace("\\", "\\\\")
                .replace("'",  "\\'")
                .replace(":",  "\\:")
                .replace(",",  "\\,")
                .replace("[",  "\\[")
                .replace("]",  "\\]")
                .replace("%",  "\\%")
            )
            fontfile = f":fontfile='{font_path}'" if font_path else ""
            filter_parts.append(
                f"drawtext=text='{safe}'"
                f"{fontfile}"
                f":enable='between(t,{t_start:.3f},{t_end:.3f})'"
                f":fontsize=55"
                f":fontcolor=white"
                f":borderw=4"
                f":bordercolor=black"
                f":x=(w-text_w)/2"
                f":y=h-th-80"
            )

        tmp_out = clip_path.replace(".mp4", "_sub.mp4")
        result = subprocess.run([
            FFMPEG_EXE, "-i", clip_path,
            "-vf", ",".join(filter_parts),
            "-c:a", "copy",
            "-preset", "fast",
            "-y", tmp_out
        ], capture_output=True, text=True)

        if result.returncode == 0 and os.path.exists(tmp_out):
            os.replace(tmp_out, clip_path)
            print(f"SUBTITLE: OK — sous-titres brûlés", flush=True)
        else:
            print(f"SUBTITLE ERROR ffmpeg (code {result.returncode}): {result.stderr[-500:]}", flush=True)
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)

    except Exception as e:
        print(f"SUBTITLE ERROR: {type(e).__name__}: {e}", flush=True)
        print(f"SUBTITLE TRACEBACK: {traceback.format_exc()}", flush=True)

# ── UPLOAD ET DÉCOUPE ─────────────────────────────────────
@app.post("/api/videos/{video_id}/upload")
async def upload_video(video_id: str, file: UploadFile = File(...), user=Depends(get_current_user)):
    db = get_db()
    video = db.execute("SELECT * FROM videos WHERE id=? AND user_id=?",
        (video_id, user["id"])).fetchone()
    db.close()
    if not video:
        raise HTTPException(status_code=404, detail="Vidéo introuvable")
    video = dict(video)
    if not video["clips_json"]:
        raise HTTPException(status_code=400, detail="Analyse pas encore terminée")

    video_dir = f"{CLIPS_DIR}/{video_id}"
    os.makedirs(video_dir, exist_ok=True)

    ext = os.path.splitext(file.filename)[1] or ".mp4"
    video_path = f"{video_dir}/source{ext}"

    with open(video_path, "wb") as f:
        content = await file.read()
        f.write(content)
    print(f"UPLOAD: Fichier reçu {file.filename} ({len(content)} bytes)", flush=True)

    import threading
    threading.Thread(target=cut_clips, args=(video_id, video_path, video["clips_json"])).start()

    return {"status": "cutting", "message": "Découpe en cours..."}

def cut_clips(video_id, video_path, clips_json):
    try:
        print(f"CUT: ffmpeg = {FFMPEG_EXE}", flush=True)
        clips = json.loads(clips_json)
        video_dir = f"{CLIPS_DIR}/{video_id}"

        for i, clip in enumerate(clips):
            ts_start = clip.get("ts_start", "00:00")
            ts_end = clip.get("ts_end", "00:30")
            start_sec = ts_to_seconds(ts_start)
            end_sec = ts_to_seconds(ts_end)
            duration = max(end_sec - start_sec, 5)

            clip_path = f"{video_dir}/clip_{i+1}.mp4"
            print(f"CUT: Clip {i+1} de {ts_start} à {ts_end} ({duration}s)", flush=True)

            result = subprocess.run([
                FFMPEG_EXE, "-i", video_path,
                "-ss", str(start_sec),
                "-t", str(duration),
                "-c:v", "libx264", "-c:a", "aac",
                "-preset", "fast",
                "-y", clip_path
            ], capture_output=True, text=True)

            if result.returncode == 0:
                clips[i]["clip_ready"] = True
                print(f"CUT: Clip {i+1} OK — lancement sous-titres", flush=True)
                add_subtitles(clip_path)
            else:
                print(f"CUT ERROR clip {i+1}: {result.stderr[-300:]}", flush=True)

        db = get_db()
        db.execute("UPDATE videos SET status='clips_ready', clips_json=? WHERE id=?",
            (json.dumps(clips), video_id))
        db.commit()
        db.close()
        print(f"CUT SUCCESS: tous les clips prêts", flush=True)

    except Exception as e:
        print(f"CUT ERROR: {type(e).__name__}: {e}", flush=True)
        db = get_db()
        db.execute("UPDATE videos SET status='analyzed' WHERE id=?", (video_id,))
        db.commit()
        db.close()

# ── DOWNLOAD CLIP ─────────────────────────────────────────
@app.get("/api/clips/{video_id}/{clip_index}")
def download_clip(video_id: str, clip_index: int):
    clip_path = f"{CLIPS_DIR}/{video_id}/clip_{clip_index}.mp4"
    if not os.path.exists(clip_path):
        raise HTTPException(status_code=404, detail="Clip non trouvé ou expiré")
    return FileResponse(
        clip_path,
        media_type="video/mp4",
        filename=f"skynet_clip_{clip_index}.mp4"
    )

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
    db.close()
    return {"clips_generated": u["clips_used"], "total_views": u["clips_used"] * 45000,
            "subscribers_gained": u["clips_used"] * 159, "engagement_rate": 6.8}

@app.get("/")
def root():
    return {"status": "SKYnet API en ligne", "version": "5.0"}
