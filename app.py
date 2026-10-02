"""
SubtitlePro — YouTube / Audio / Video  →  Subtitle + Bangla Translation
Designed & Developed by Md Ismail Hossain Nahin

Features
  1. YouTube-র নিজস্ব Subtitle (Caption) আনে
  2. Subtitle না থাকলে AI (Whisper) দিয়ে নতুন Subtitle বানায়  (YouTube link বা নিজের File)
  3. বাংলা বাদে অন্য ভাষা থাকলে Google Translate দিয়ে ঠিকঠাক বাংলায় অনুবাদ করে
  4. Background job + live progress, Cancel সুবিধা
"""
import html
import os
import re
import json
import time
import uuid
import shutil
import tempfile
import threading
import urllib.parse
import urllib.request
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from youtube_transcript_api import YouTubeTranscriptApi

# ---- optional libraries (না থাকলেও app চলবে, শুধু সংশ্লিষ্ট feature বন্ধ থাকবে) ----
try:
    from deep_translator import GoogleTranslator
except Exception:  # pragma: no cover
    GoogleTranslator = None

try:
    import yt_dlp
except Exception:  # pragma: no cover
    yt_dlp = None

try:
    from faster_whisper import WhisperModel
except Exception:  # pragma: no cover
    WhisperModel = None

CREDIT = "Md Ismail Hossain Nahin"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_AI_MINUTES = int(os.environ.get("MAX_AI_MINUTES", "120"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "1024"))

# NOTE: static_folder=None  → app.py / অন্য ফাইল ব্রাউজার থেকে খোলা যাবে না (security)
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
api = YouTubeTranscriptApi()

Seg = namedtuple("Seg", "start end text")


# ════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════
class JobCancelled(Exception):
    pass


class JobError(Exception):
    def __init__(self, message, code="failed"):
        super().__init__(message)
        self.message = message
        self.code = code


def video_id(url: str):
    url = (url or "").strip()
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    return url if re.fullmatch(r"[A-Za-z0-9_-]{11}", url) else None


def fmt(sec: float) -> str:
    s = int(sec)
    h, m, s2 = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}:{m:02d}:{s2:02d}"
    return f"{m}:{s2:02d}"


def clean(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    return text


def bengali_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    bn = sum(1 for c in letters if "\u0980" <= c <= "\u09ff")
    return bn / len(letters)


def get_title(url):
    try:
        q = urllib.parse.quote(url, safe="")
        with urllib.request.urlopen(
            f"https://www.youtube.com/oembed?url={q}&format=json", timeout=8
        ) as r:
            return json.loads(r.read().decode())["title"]
    except Exception:
        return "YouTube Video Transcript"


def bucketize(segs, step):
    """ছোট ছোট Subtitle-কে `step` সেকেন্ডের row-তে জোড়া লাগায়।"""
    buckets = {}
    for s in segs:
        b = int(s.start // step)
        d = buckets.setdefault(b, {"start": s.start, "end": s.end, "texts": []})
        t = clean(s.text)
        if t:
            d["texts"].append(t)
        d["start"] = min(d["start"], s.start)
        d["end"] = max(d["end"], s.end)
    rows = []
    for b in sorted(buckets):
        d = buckets[b]
        text = " ".join(d["texts"]).strip()
        if not text:
            continue
        rows.append({
            "time": fmt(d["start"]),
            "sec": round(d["start"], 2),
            "end": round(max(d["end"], d["start"] + 0.5), 2),
            "text": text,
            "translation": "",
        })
    return rows


# ════════════════════════════════════════════════════════════════
#  Job system  (background thread + progress polling)
# ════════════════════════════════════════════════════════════════
class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.status = "queued"      # queued | running | done | error | cancelled
        self.progress = 0.0
        self.stage = "queued"
        self.message = "অপেক্ষা করুন..."
        self.result = None
        self.error = None
        self.code = None
        self.cancel = threading.Event()
        self.created = time.time()

    def update(self, progress=None, message=None, stage=None):
        if self.cancel.is_set():
            raise JobCancelled()
        if progress is not None:
            self.progress = max(self.progress, min(99.0, float(progress)))
        if message:
            self.message = message
        if stage:
            self.stage = stage

    def public(self):
        d = {
            "id": self.id,
            "status": self.status,
            "progress": round(self.progress, 1),
            "stage": self.stage,
            "message": self.message,
        }
        if self.status == "done":
            d["progress"] = 100
            d["result"] = self.result
        if self.status == "error":
            d["error"] = self.error
            d["code"] = self.code
        return d


JOBS = {}
JOBS_LOCK = threading.Lock()


def _gc_jobs():
    now = time.time()
    with JOBS_LOCK:
        for k in [k for k, j in JOBS.items() if now - j.created > 3600]:
            JOBS.pop(k, None)


def start_job(fn, *args):
    _gc_jobs()
    job = Job()
    with JOBS_LOCK:
        JOBS[job.id] = job

    def runner():
        job.status = "running"
        try:
            job.result = fn(job, *args)
            job.status = "done"
        except JobCancelled:
            job.status = "cancelled"
            job.message = "বাতিল করা হয়েছে"
        except JobError as e:
            job.status, job.error, job.code = "error", e.message, e.code
        except Exception as e:  # unexpected
            job.status, job.code = "error", "failed"
            job.error = f"সমস্যা হয়েছে: {type(e).__name__} — {e}"

    threading.Thread(target=runner, daemon=True).start()
    return job


# ════════════════════════════════════════════════════════════════
#  Translation  (Google Translate → fallback: YouTube auto-translate)
# ════════════════════════════════════════════════════════════════
def _needs_translation(text, target):
    if not text.strip():
        return False
    if target.split("-")[0] == "bn" and bengali_ratio(text) > 0.6:
        return False  # আগে থেকেই বাংলা
    return True


def _translate_chunk(translator, chunk):
    """chunk = list[str]; একসাথে পাঠিয়ে লাইন-সংখ্যা মিলিয়ে দেখে, না মিললে একটা একটা করে।"""
    for attempt in range(3):
        try:
            joined = "\n".join(chunk)
            out = translator.translate(joined)
            parts = (out or "").split("\n")
            if len(parts) == len(chunk):
                return [p.strip() for p in parts]
            break
        except Exception:
            time.sleep(0.6 * (attempt + 1))
    res = []
    for t in chunk:
        done = ""
        for attempt in range(2):
            try:
                done = (translator.translate(t) or "").strip()
                break
            except Exception:
                time.sleep(0.5)
        res.append(done)
    return res


def google_translate(texts, target, job=None, p0=0, p1=100):
    """texts → translated list (ব্যর্থ হলে ""). Bengali লেখা skip করে।"""
    if GoogleTranslator is None:
        raise JobError("deep-translator install করা নেই", "translator_missing")
    target_code = {"zh": "zh-CN"}.get(target, target)
    out = [""] * len(texts)
    todo = []
    for i, t in enumerate(texts):
        if _needs_translation(t, target):
            todo.append(i)
        else:
            out[i] = t  # already target language
    if not todo:
        return out

    # chunk ≤ 4000 char
    chunks, cur, cur_len = [], [], 0
    for i in todo:
        l = len(texts[i]) + 1
        if cur and cur_len + l > 4000:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(i)
        cur_len += l
    if cur:
        chunks.append(cur)

    done_count = 0
    lock = threading.Lock()

    def work(idx_list):
        nonlocal done_count
        if job and job.cancel.is_set():
            return idx_list, [""] * len(idx_list)
        tr = GoogleTranslator(source="auto", target=target_code)
        res = _translate_chunk(tr, [texts[i] for i in idx_list])
        with lock:
            done_count += 1
            if job:
                pct = p0 + (p1 - p0) * done_count / len(chunks)
                job.progress = max(job.progress, min(99.0, pct))
        return idx_list, res

    with ThreadPoolExecutor(max_workers=4) as ex:
        for idx_list, res in ex.map(work, chunks):
            for i, r in zip(idx_list, res):
                out[i] = r
    if job:
        job.update()  # cancel হলে এখানে exception
    return out


def youtube_translate(tr_obj, target, step, rows):
    """Fallback: YouTube-এর নিজস্ব auto-translate (যদি Google Translate কাজ না করে)।"""
    segs = [Seg(s.start, s.start + s.duration, s.text) for s in tr_obj.translate(target).fetch()]
    tb = bucketize(segs, step)
    res = []
    for r in rows:
        best = next((t for t in tb if abs(t["sec"] - r["sec"]) < step), None)
        res.append(best["text"] if best else "")
    return res


def apply_translation(job, rows, src_code, target, step, tr_obj=None):
    """rows-এ 'translation' বসায়। (translated?, engine) ফেরত দেয়।"""
    if not target or not rows:
        return False, ""
    if src_code and src_code.split("-")[0] == target.split("-")[0]:
        return False, ""  # source = target
    job.update(55, "বাংলায় অনুবাদ করা হচ্ছে..." if target == "bn" else "অনুবাদ করা হচ্ছে...", "translate")
    texts = [r["text"] for r in rows]
    engine = "Google Translate"
    try:
        trans = google_translate(texts, target, job, 55, 97)
        # প্রায় সবই খালি এলে ব্যর্থ ধরে নিই
        if sum(1 for t in trans if t) < max(1, len(trans) * 0.5):
            raise JobError("translation mostly empty", "translate_failed")
    except JobCancelled:
        raise
    except Exception:
        trans = None
        if tr_obj is not None and getattr(tr_obj, "is_translatable", False):
            try:
                trans = youtube_translate(tr_obj, target, step, rows)
                engine = "YouTube Auto-translate"
            except Exception:
                trans = None
    if not trans:
        return False, ""
    for r, t in zip(rows, trans):
        r["translation"] = t
    return True, engine


# ════════════════════════════════════════════════════════════════
#  Pipeline 1 — YouTube Captions
# ════════════════════════════════════════════════════════════════
def classify_yt_error(e):
    n = type(e).__name__
    if n in ("TranscriptsDisabled", "NoTranscriptFound"):
        return "no_captions", "এই ভিডিওতে কোনো Subtitle (Caption) নেই। নিচের বাটন দিয়ে AI দিয়ে Subtitle বানাতে পারেন।"
    if n in ("IpBlocked", "RequestBlocked"):
        return "blocked", "YouTube এই মুহূর্তে অনুরোধ আটকে দিয়েছে। কিছুক্ষণ পর চেষ্টা করুন, অথবা AI Generate ব্যবহার করুন।"
    if n in ("VideoUnavailable", "VideoUnplayable", "InvalidVideoId", "AgeRestricted"):
        return "unavailable", "ভিডিওটি পাওয়া যাচ্ছে না (private / deleted / age-restricted হতে পারে)।"
    return "failed", f"Subtitle আনা যায়নি: {n} — {e}"


def job_captions(job, url, src, target, step):
    vid = video_id(url)
    if not vid:
        raise JobError("সঠিক YouTube link দিন", "invalid_url")
    job.update(5, "ভিডিও চেক করা হচ্ছে...", "fetch")
    try:
        tlist = api.list(vid)
        tr = None
        if src:
            try:
                tr = tlist.find_transcript([src])
            except Exception:
                tr = None
        if tr is None:
            tr = next(iter(tlist))  # প্রথমে manual, তারপর auto-generated
        job.update(20, "Subtitle ডাউনলোড হচ্ছে...", "fetch")
        raw = tr.fetch()
    except JobCancelled:
        raise
    except Exception as e:
        code, msg = classify_yt_error(e)
        raise JobError(msg, code)

    segs = [Seg(s.start, s.start + s.duration, s.text) for s in raw]
    rows = bucketize(segs, step)
    if not rows:
        raise JobError("Subtitle খালি পাওয়া গেছে", "no_captions")
    job.update(40, "Row সাজানো হচ্ছে...", "fetch")
    title = get_title(url)

    translated, engine = apply_translation(job, rows, tr.language_code, target, step, tr)
    return {
        "title": title,
        "source": "youtube",
        "video_id": vid,
        "language": tr.language,
        "language_code": tr.language_code,
        "auto_generated": bool(tr.is_generated),
        "generated_by": "captions",
        "target": target,
        "translated": translated,
        "engine": engine,
        "rows": rows,
    }


# ════════════════════════════════════════════════════════════════
#  Pipeline 2 — AI Subtitle (yt-dlp + faster-whisper)
# ════════════════════════════════════════════════════════════════
_MODELS = {}
_MODELS_LOCK = threading.Lock()
_WHISPER_RUN_LOCK = threading.Lock()  # একসাথে একটাই transcribe (RAM বাঁচাতে)


def _device():
    try:
        import ctranslate2
        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


def get_model(size):
    with _MODELS_LOCK:
        if size not in _MODELS:
            dev = _device()
            _MODELS[size] = WhisperModel(
                size, device=dev, compute_type="float16" if dev == "cuda" else "int8"
            )
        return _MODELS[size]


def ai_available():
    return WhisperModel is not None


def download_audio(job, url, workdir):
    if yt_dlp is None:
        raise JobError("yt-dlp install করা নেই (pip install yt-dlp)", "ai_unavailable")

    def hook(d):
        if job.cancel.is_set():
            raise JobCancelled()
        if d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if tot:
                job.progress = max(job.progress, 5 + 25 * d.get("downloaded_bytes", 0) / tot)

    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": os.path.join(workdir, "audio.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 20,
        "progress_hooks": [hook],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            dur = info.get("duration") or 0
            if dur and dur > MAX_AI_MINUTES * 60:
                raise JobError(
                    f"ভিডিও অনেক বড় ({int(dur // 60)} মিনিট)। সর্বোচ্চ {MAX_AI_MINUTES} মিনিট পর্যন্ত AI Subtitle সম্ভব।",
                    "too_long",
                )
            job.update(6, "অডিও ডাউনলোড হচ্ছে...", "download")
            ydl.process_ids = None
            ydl.download([url])
    except (JobCancelled, JobError):
        raise
    except Exception as e:
        raise JobError(f"ভিডিও ডাউনলোড করা যায়নি: {str(e)[:200]}", "download_failed")

    files = [f for f in os.listdir(workdir) if f.startswith("audio.")]
    if not files:
        raise JobError("অডিও ফাইল পাওয়া যায়নি", "download_failed")
    return os.path.join(workdir, files[0]), info.get("title") or "YouTube Video", info.get("duration") or 0


def transcribe(job, path, src, model_size, duration_hint=0):
    if not ai_available():
        raise JobError(
            "AI Subtitle চালু নেই। Terminal-এ লিখুন:  pip install faster-whisper yt-dlp",
            "ai_unavailable",
        )
    job.update(32, f"AI Model লোড হচ্ছে ({model_size})... প্রথমবার একটু সময় লাগবে", "model")
    model = get_model(model_size)
    lang = (src or "").split("-")[0] or None
    job.update(36, "AI শুনে Subtitle লিখছে...", "transcribe")
    with _WHISPER_RUN_LOCK:
        gen, info = model.transcribe(
            path, language=lang, vad_filter=True, beam_size=3, condition_on_previous_text=False
        )
        total = info.duration or duration_hint or 0
        if total and total > MAX_AI_MINUTES * 60:
            raise JobError(f"মিডিয়া অনেক বড় (সর্বোচ্চ {MAX_AI_MINUTES} মিনিট)", "too_long")
        segs = []
        for s in gen:
            if job.cancel.is_set():
                raise JobCancelled()
            segs.append(Seg(s.start, s.end, s.text))
            if total:
                pct = 36 + 44 * min(1.0, s.end / total)
                job.progress = max(job.progress, pct)
                job.message = f"AI Subtitle লিখছে... {fmt(s.end)} / {fmt(total)}"
    return segs, info.language, (info.language_probability or 0)


def job_ai(job, kind, source, src, target, step, model_size, display_title=None):
    """kind = 'url' | 'file'"""
    workdir = tempfile.mkdtemp(prefix="subpro_")
    try:
        vid = None
        if kind == "url":
            vid = video_id(source)
            if not vid:
                raise JobError("সঠিক YouTube link দিন", "invalid_url")
            job.update(3, "ভিডিও তথ্য আনা হচ্ছে...", "download")
            path, title, dur = download_audio(job, f"https://www.youtube.com/watch?v={vid}", workdir)
        else:
            path, title, dur = source, display_title or "Uploaded Media", 0

        segs, lang_code, prob = transcribe(job, path, src, model_size, dur)
        rows = bucketize(segs, step)
        if not rows:
            raise JobError("কোনো কথা শনাক্ত করা যায়নি (অডিও খালি / শুধু মিউজিক হতে পারে)", "no_speech")

        translated, engine = apply_translation(job, rows, lang_code, target, step, None)
        return {
            "title": title,
            "source": "youtube" if vid else "file",
            "video_id": vid,
            "language": LANG_NAMES.get(lang_code, lang_code),
            "language_code": lang_code,
            "auto_generated": True,
            "generated_by": f"whisper-{model_size}",
            "target": target,
            "translated": translated,
            "engine": engine,
            "rows": rows,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


LANG_NAMES = {
    "bn": "বাংলা", "en": "English", "hi": "हिन्दी", "ar": "العربية", "ur": "اردو",
    "es": "Español", "fr": "Français", "de": "Deutsch", "ja": "日本語", "ko": "한국어",
    "zh": "中文", "ru": "Русский", "tr": "Türkçe", "id": "Indonesia", "ms": "Melayu",
    "ta": "தமிழ்", "pt": "Português", "it": "Italiano", "ne": "नेपाली", "fa": "فارسی",
}


# ════════════════════════════════════════════════════════════════
#  Pipeline 3 — শুধু অনুবাদ (ফলাফল আসার পর ভাষা বদলাতে)
# ════════════════════════════════════════════════════════════════
def job_translate(job, texts, target):
    job.update(10, "অনুবাদ করা হচ্ছে...", "translate")
    out = google_translate(texts, target, job, 10, 98)
    return {"translations": out, "target": target, "engine": "Google Translate"}


# ════════════════════════════════════════════════════════════════
#  Routes
# ════════════════════════════════════════════════════════════════
def _step(v, default=5):
    try:
        return max(2, min(60, int(v)))
    except Exception:
        return default


def _model(v):
    return v if v in ("tiny", "base", "small", "medium") else "base"


@app.after_request
def headers(resp):
    resp.headers["X-Developer"] = CREDIT
    resp.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"
    return resp


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/health")
def health():
    return jsonify(
        ok=True,
        developer=CREDIT,
        translate=GoogleTranslator is not None,
        ai=ai_available(),
        ytdlp=yt_dlp is not None,
        max_ai_minutes=MAX_AI_MINUTES,
        max_upload_mb=MAX_UPLOAD_MB,
    )


@app.post("/api/start")
def start():
    d = request.get_json(silent=True) or {}
    url = (d.get("url") or "").strip()
    if not video_id(url):
        return jsonify(error="সঠিক YouTube link দিন", code="invalid_url"), 400
    mode = d.get("mode", "captions")
    src = (d.get("lang") or "").strip()
    target = (d.get("translate") or "").strip()
    step = _step(d.get("step"))
    if mode == "ai":
        if not ai_available() or yt_dlp is None:
            return jsonify(
                error="AI Subtitle চালু নেই। Terminal-এ লিখুন:  pip install faster-whisper yt-dlp",
                code="ai_unavailable",
            ), 501
        job = start_job(job_ai, "url", url, src, target, step, _model(d.get("model")))
    else:
        job = start_job(job_captions, url, src, target, step)
    return jsonify(job.public()), 202


@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="কোনো ফাইল পাওয়া যায়নি", code="no_file"), 400
    if not ai_available():
        return jsonify(
            error="AI Subtitle চালু নেই। Terminal-এ লিখুন:  pip install faster-whisper yt-dlp",
            code="ai_unavailable",
        ), 501
    ext = os.path.splitext(secure_filename(f.filename))[1].lower() or ".bin"
    workdir = tempfile.mkdtemp(prefix="subpro_up_")
    path = os.path.join(workdir, "media" + ext)
    f.save(path)
    src = (request.form.get("lang") or "").strip()
    target = (request.form.get("translate") or "").strip()
    step = _step(request.form.get("step"))
    model = _model(request.form.get("model"))
    title = os.path.splitext(f.filename)[0][:120]

    def run(job):
        try:
            return job_ai(job, "file", path, src, target, step, model, title)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    job = start_job(run)
    return jsonify(job.public()), 202


@app.post("/api/translate")
def translate_api():
    d = request.get_json(silent=True) or {}
    texts = d.get("texts")
    target = (d.get("target") or "").strip()
    if not isinstance(texts, list) or not target or len(texts) > 5000:
        return jsonify(error="ভুল অনুরোধ", code="bad_request"), 400
    texts = [clean(str(t))[:1500] for t in texts]
    if GoogleTranslator is None:
        return jsonify(
            error="Translator চালু নেই। Terminal-এ লিখুন:  pip install deep-translator",
            code="translator_missing",
        ), 501
    return jsonify(start_job(job_translate, texts, target).public()), 202


@app.get("/api/job/<jid>")
def job_status(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify(error="Job পাওয়া যায়নি (মেয়াদ শেষ হতে পারে)", code="not_found"), 404
    return jsonify(job.public())


@app.post("/api/job/<jid>/cancel")
def job_cancel(jid):
    job = JOBS.get(jid)
    if job:
        job.cancel.set()
    return jsonify(ok=True)


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n  SubtitlePro  •  Designed & Developed by {CREDIT}")
    print(f"  Open:  http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}")
    print(f"  Translate: {'ON' if GoogleTranslator else 'OFF (pip install deep-translator)'}"
          f"   AI Subtitle: {'ON' if ai_available() and yt_dlp else 'OFF (pip install faster-whisper yt-dlp)'}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
