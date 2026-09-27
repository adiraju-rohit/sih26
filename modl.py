"""
26088 - backend logic module.
Handles: user accounts + per-account chat history (sqlite), the local
knowledge base (TF-IDF, used only in "document" answer mode), and every
external API call - Groq (speech recognition, web-search answers,
translation, English-only TTS) and Bhashini/ULCA (translation, and the
top-priority speech recognition + text-to-speech backend for Indian
languages).
"""

import os
import re
import math
import time
import base64
import shutil
import sqlite3
import tempfile
import subprocess
from collections import Counter

import requests
from dotenv import load_dotenv

load_dotenv()

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_ON_VERCEL = bool(os.environ.get("VERCEL"))

# Vercel's filesystem is read-only outside /tmp, and /tmp is wiped between
# cold starts - fine for a demo, but chat history won't be durable there.
# For a real deployment, point DB_PATH at a persistent volume or swap the
# sqlite3 calls below for a hosted database.
if _ON_VERCEL:
    DB_PATH = os.path.join(tempfile.gettempdir(), "app.db")
else:
    DB_PATH = os.environ.get("DB_PATH", "") or "app.db"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
KNOWLEDGE_FILE = os.environ.get("KNOWLEDGE_FILE") or os.path.join(_BASE_DIR, "data", "knowledge.txt")
if not os.path.isabs(KNOWLEDGE_FILE):
    KNOWLEDGE_FILE = os.path.join(_BASE_DIR, KNOWLEDGE_FILE)

CHAT_MODEL = os.environ.get("GROQ_CHAT_MODEL", "openai/gpt-oss-120b")
STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3")
# groq/compound is a Groq "system" (not a plain chat model) that can
# decide on its own to run a real web search before answering.
SEARCH_MODEL = os.environ.get("GROQ_SEARCH_MODEL", "groq/compound")
# Groq's hosted TTS. English-only - see GROQ_TTS_LANGS below.
GROQ_TTS_MODEL = os.environ.get("GROQ_TTS_MODEL", "playai-tts")
GROQ_TTS_VOICE = os.environ.get("GROQ_TTS_VOICE", "Fritz-PlayAI")

_groq_client = None
if GROQ_API_KEY:
    try:
        from groq import Groq
        _groq_client = Groq(api_key=GROQ_API_KEY)
    except Exception as e:
        print(f"[modl] WARNING: failed to initialize Groq client: {e}")
        _groq_client = None


# ---------------------------------------------------------------------------
# Supported languages: Indian languages only, plus English (which is also
# an official language of India and the pivot language everything gets
# translated through). Urdu is intentionally NOT included. Anything
# detected outside this set is clamped to English rather than passed
# through, so a stray/misdetected language never reaches translation or
# TTS with an unsupported code.
# ---------------------------------------------------------------------------

ALLOWED_LANGS = {"en", "hi", "bn", "mr", "te", "ta", "gu", "kn", "ml", "pa"}

LANG_NAMES = {
    "en": "English", "hi": "Hindi", "bn": "Bengali", "mr": "Marathi",
    "te": "Telugu", "ta": "Tamil", "gu": "Gujarati", "kn": "Kannada",
    "ml": "Malayalam", "pa": "Punjabi",
}

LANG_TO_BCP47 = {
    "en": "en-IN", "hi": "hi-IN", "bn": "bn-IN", "mr": "mr-IN", "te": "te-IN",
    "ta": "ta-IN", "gu": "gu-IN", "kn": "kn-IN", "ml": "ml-IN", "pa": "pa-IN",
}


def _clamp_lang(lang_code):
    code = (lang_code or "en")[:2].lower()
    return code if code in ALLOWED_LANGS else "en"


def bcp47_for_lang(lang_code):
    return LANG_TO_BCP47[_clamp_lang(lang_code)]


# ---------------------------------------------------------------------------
# Answer mode toggle: "web" (Groq Compound searches the live web) vs
# "document" (Groq answers only from the local knowledge.txt file).
# Runtime-toggleable via GET/POST /api/settings/answer_mode in app.py - no
# restart needed. ANSWER_MODE in the environment just sets the starting
# value.
# ---------------------------------------------------------------------------

_VALID_ANSWER_MODES = ("web", "document")
_answer_mode = os.environ.get("ANSWER_MODE", "web").strip().lower()
if _answer_mode not in _VALID_ANSWER_MODES:
    _answer_mode = "web"


def get_answer_mode():
    return _answer_mode


def set_answer_mode(mode):
    global _answer_mode
    mode = (mode or "").strip().lower()
    if mode not in _VALID_ANSWER_MODES:
        raise ValueError(f"mode must be one of {_VALID_ANSWER_MODES}, got {mode!r}")
    _answer_mode = mode
    return _answer_mode


# ---------------------------------------------------------------------------
# Database: chat history. There are no accounts - everything is stored
# under one shared conversation (see CHAT_USER in app.py), matching the
# ESP32 device's own separate conversation stored the same way.
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    try:
        conn = get_conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            lang TEXT NOT NULL DEFAULT 'en',
            source TEXT NOT NULL DEFAULT 'web',
            ts INTEGER NOT NULL DEFAULT 0)""")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[modl] WARNING: init_db() failed (DB_PATH={DB_PATH!r}): {e}")


def add_message(username, role, text, lang="en", source="web"):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO messages (username, role, text, lang, source, ts) VALUES (?,?,?,?,?,?)",
        (username, role, text, lang, source, int(time.time())),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def get_messages(username, limit=200):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, role, text, lang, source, ts FROM messages WHERE username=? ORDER BY ts ASC, id ASC LIMIT ?",
        (username, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_messages(username):
    conn = get_conn()
    conn.execute("DELETE FROM messages WHERE username=?", (username,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Local knowledge base (TF-IDF chunk retrieval) - used only when the answer
# mode above is "document".
# ---------------------------------------------------------------------------

_STOPWORDS = set("""a an the is are was were be been being of to in on for and or
but if then so than that this these those it its as by with from at about into
your you i we our us they he she his her them do does did can could should would
will shall not no yes what when where who whom which how""".split())


def _tokenize(text):
    return [w for w in re.findall(r"[a-zA-Z]+", text.lower()) if w not in _STOPWORDS and len(w) > 2]


class KnowledgeBase:
    """Splits a text file into topic chunks and answers similarity queries
    with TF-IDF cosine similarity - no external services needed."""

    def __init__(self, path):
        self.path = path
        self.chunks = []
        self._doc_freq = Counter()
        self._chunk_vectors = []
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            self.chunks = []
            return
        with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()

        if "TOPIC:" in raw:
            parts = re.split(r"(?=TOPIC:)", raw)
            self.chunks = [p.strip() for p in parts if p.strip()]
        else:
            paras = [p.strip() for p in raw.split("\n\n") if p.strip()]
            self.chunks, buf = [], ""
            for p in paras:
                if len(buf) + len(p) < 900:
                    buf += ("\n" + p if buf else p)
                else:
                    if buf:
                        self.chunks.append(buf)
                    buf = p
            if buf:
                self.chunks.append(buf)

        tokenized = [_tokenize(c) for c in self.chunks]
        for toks in tokenized:
            for w in set(toks):
                self._doc_freq[w] += 1

        n_docs = max(len(self.chunks), 1)
        self._chunk_vectors = []
        for toks in tokenized:
            tf = Counter(toks)
            vec = {}
            for w, count in tf.items():
                idf = math.log((n_docs + 1) / (self._doc_freq[w] + 1)) + 1
                vec[w] = (1 + math.log(count)) * idf
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            self._chunk_vectors.append((vec, norm))

    def reload(self):
        self._load()

    def _query_vector(self, query):
        toks = _tokenize(query)
        n_docs = max(len(self.chunks), 1)
        tf = Counter(toks)
        vec = {}
        for w, count in tf.items():
            idf = math.log((n_docs + 1) / (self._doc_freq.get(w, 0) + 1)) + 1
            vec[w] = (1 + math.log(count)) * idf
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return vec, norm

    def search(self, query, top_k=3):
        if not self.chunks:
            return []
        qvec, qnorm = self._query_vector(query)
        scores = []
        for i, (vec, norm) in enumerate(self._chunk_vectors):
            common = set(qvec) & set(vec)
            dot = sum(qvec[w] * vec[w] for w in common)
            sim = dot / (qnorm * norm)
            if sim > 0:
                scores.append((sim, i))
        scores.sort(reverse=True)
        return [self.chunks[i] for _, i in scores[:top_k]]


try:
    _kb = KnowledgeBase(KNOWLEDGE_FILE)
except Exception as e:
    print(f"[modl] WARNING: failed to load knowledge base from {KNOWLEDGE_FILE!r}: {e}")
    _kb = KnowledgeBase.__new__(KnowledgeBase)
    _kb.path, _kb.chunks, _kb._doc_freq, _kb._chunk_vectors = KNOWLEDGE_FILE, [], Counter(), []


def kb_context(query, top_k=4):
    results = _kb.search(query, top_k=top_k)
    return "\n\n".join(results) if results else ""


def reload_knowledge_base():
    """Re-reads KNOWLEDGE_FILE from disk without a restart."""
    _kb.reload()


# ---------------------------------------------------------------------------
# Bhashini (ULCA) - HIGHEST PRIORITY for both speech-to-text and
# text-to-speech. Falls back to Groq automatically on any missing config
# or request failure, so the app keeps working either way.
#
# Required env vars: BHASHINI_USER_ID, BHASHINI_ULCA_API_KEY.
#
# One real constraint this code works around: Bhashini's ASR needs the
# source language specified up front - it can't auto-detect the spoken
# language the way Whisper can, and automatic language detection is a
# core requirement here. So transcribe_and_translate() below still makes
# one quick Groq Whisper call first purely to detect which language was
# spoken; once that's known, Bhashini (if configured) is used to produce
# the actual transcript and the English translation, since that's what
# should have priority. Groq's own transcript/translation is only used if
# Bhashini isn't configured or a Bhashini call fails.
# ---------------------------------------------------------------------------

BHASHINI_USER_ID = os.environ.get("BHASHINI_USER_ID", "").strip()
BHASHINI_ULCA_API_KEY = os.environ.get("BHASHINI_ULCA_API_KEY", "").strip()
BHASHINI_PIPELINE_ID = os.environ.get("BHASHINI_PIPELINE_ID", "64392f96daac500b55c543cd")
BHASHINI_CONFIG_URL = "https://meity-auth.ulcacontrib.org/ulca/apis/v0/model/getModelsPipeline"
_BHASHINI_TIMEOUT = 25

_bhashini_pipeline_cache = {}


def bhashini_configured():
    return bool(BHASHINI_USER_ID and BHASHINI_ULCA_API_KEY)


def _bhashini_get_pipeline(task_type, source_lang, target_lang=None):
    """Looks up (and caches) the callback URL, auth header, and serviceId
    for one Bhashini pipeline task. Raises on any failure."""
    cache_key = (task_type, source_lang, target_lang)
    if cache_key in _bhashini_pipeline_cache:
        return _bhashini_pipeline_cache[cache_key]

    language_cfg = {"sourceLanguage": source_lang}
    if target_lang:
        language_cfg["targetLanguage"] = target_lang

    resp = requests.post(
        BHASHINI_CONFIG_URL,
        headers={
            "userID": BHASHINI_USER_ID,
            "ulcaApiKey": BHASHINI_ULCA_API_KEY,
            "Content-Type": "application/json",
        },
        json={
            "pipelineTasks": [{"taskType": task_type, "config": {"language": language_cfg}}],
            "pipelineRequestConfig": {"pipelineId": BHASHINI_PIPELINE_ID},
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    endpoint = data["pipelineInferenceAPIEndPoint"]
    auth = endpoint["inferenceApiKey"]
    callback_url = endpoint["callbackUrl"]
    headers = {auth["name"]: auth["value"], "Content-Type": "application/json"}
    service_id = data["pipelineResponseConfig"][0]["config"][0]["serviceId"]

    result = (callback_url, headers, service_id)
    _bhashini_pipeline_cache[cache_key] = result
    return result


def bhashini_translate(text, source_lang, target_lang):
    if source_lang == target_lang or not text:
        return text
    callback_url, headers, service_id = _bhashini_get_pipeline("translation", source_lang, target_lang)
    body = {
        "pipelineTasks": [{
            "taskType": "translation",
            "config": {
                "language": {"sourceLanguage": source_lang, "targetLanguage": target_lang},
                "serviceId": service_id,
            },
        }],
        "inputData": {"input": [{"source": text}]},
    }
    resp = requests.post(callback_url, headers=headers, json=body, timeout=_BHASHINI_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["pipelineResponse"][0]["output"][0]["target"]


def bhashini_tts(text, lang, out_wav_path):
    callback_url, headers, service_id = _bhashini_get_pipeline("tts", lang)
    body = {
        "pipelineTasks": [{
            "taskType": "tts",
            "config": {
                "language": {"sourceLanguage": lang},
                "serviceId": service_id,
                "gender": "female",
                "samplingRate": 16000,
            },
        }],
        "inputData": {"input": [{"source": text}]},
    }
    resp = requests.post(callback_url, headers=headers, json=body, timeout=_BHASHINI_TIMEOUT)
    resp.raise_for_status()
    audio_b64 = resp.json()["pipelineResponse"][0]["audio"][0]["audioContent"]
    with open(out_wav_path, "wb") as f:
        f.write(base64.b64decode(audio_b64))


def bhashini_asr(audio_path, lang, audio_format="wav"):
    """Transcribes `audio_path` (already known to be `lang`) via Bhashini.
    Note: Bhashini's ASR service is built around WAV/FLAC/PCM audio - the
    ESP32 device always sends WAV, so this works reliably for it; browser
    recordings are WebM, which Bhashini may reject, in which case this
    raises and the caller falls back to the Groq transcript it already
    has (see transcribe_and_translate() below)."""
    callback_url, headers, service_id = _bhashini_get_pipeline("asr", lang)
    with open(audio_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode()
    body = {
        "pipelineTasks": [{
            "taskType": "asr",
            "config": {
                "language": {"sourceLanguage": lang},
                "serviceId": service_id,
                "audioFormat": audio_format,
                "samplingRate": 16000,
            },
        }],
        "inputData": {"audio": [{"audioContent": audio_b64}]},
    }
    resp = requests.post(callback_url, headers=headers, json=body, timeout=_BHASHINI_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["pipelineResponse"][0]["output"][0]["source"]


# ---------------------------------------------------------------------------
# Speech-to-text: Groq Whisper always runs once, purely to auto-detect the
# spoken language (Bhashini can't do that up front). Once the language is
# known, Bhashini - if configured - is tried FIRST to produce the actual
# transcript and English translation; Groq's own results (already in
# hand from the detection call) are the automatic fallback.
# ---------------------------------------------------------------------------

def stt_available():
    return bhashini_configured() or _groq_client is not None


def _audio_format_from_path(audio_path):
    ext = os.path.splitext(audio_path)[1].lstrip(".").lower()
    return ext if ext in ("wav", "flac", "pcm", "mp3") else "wav"


def transcribe_and_translate(audio_path):
    """Returns dict: {text_original, lang, text_english}."""
    if not _groq_client and not bhashini_configured():
        raise RuntimeError("No speech-to-text backend is configured. Set GROQ_API_KEY and/or BHASHINI_USER_ID/BHASHINI_ULCA_API_KEY.")

    text_original_groq = ""
    text_english_groq = ""
    detected_lang = "en"

    if _groq_client:
        try:
            with open(audio_path, "rb") as f:
                transcription = _groq_client.audio.transcriptions.create(
                    file=f, model=STT_MODEL, response_format="verbose_json",
                )
            text_original_groq = (transcription.text or "").strip()
            detected_lang_raw = getattr(transcription, "language", "en") or "en"
            detected_lang = _clamp_lang(detected_lang_raw)
            if detected_lang_raw[:2].lower() != detected_lang:
                print(f"[modl] transcribe: Whisper detected {detected_lang_raw!r}, "
                      f"which isn't one of {sorted(ALLOWED_LANGS)} - treating as English.")
        except Exception as e:
            print(f"[modl] WARNING: Groq speech recognition failed: {e}")

    # Bhashini gets priority for the actual transcript, now that we know
    # the language (from Groq above, or English by default if Groq isn't
    # configured at all).
    text_original = None
    if bhashini_configured():
        try:
            audio_fmt = _audio_format_from_path(audio_path)
            text_original = bhashini_asr(audio_path, detected_lang, audio_fmt)
        except Exception as e:
            print(f"[modl] Bhashini ASR failed ({e}), using Groq's transcript instead.")

    if not text_original:
        text_original = text_original_groq

    if detected_lang == "en":
        text_english = text_original
    else:
        text_english = None
        if bhashini_configured() and text_original:
            try:
                text_english = bhashini_translate(text_original, detected_lang, "en")
            except Exception as e:
                print(f"[modl] Bhashini translate-to-English failed ({e}), falling back to Groq.")

        if not text_english and _groq_client:
            try:
                with open(audio_path, "rb") as f:
                    translation = _groq_client.audio.translations.create(
                        file=f, model=STT_MODEL, response_format="verbose_json",
                    )
                text_english = (translation.text or "").strip()
            except Exception as e:
                print(f"[modl] WARNING: Groq translation-to-English failed: {e}")

        if not text_english:
            text_english = text_original  # last resort - better than nothing

    return {"text_original": text_original or "", "lang": detected_lang, "text_english": text_english or ""}


# ---------------------------------------------------------------------------
# Groq: answer generation - either from a live web search or from the
# local knowledge base, depending on get_answer_mode().
# ---------------------------------------------------------------------------

def _answer_from_document(question_en, hist_ctx):
    context = kb_context(question_en, top_k=4)
    system_prompt = (
        "You are a helpful assistant. Answer questions using the reference "
        "material provided; if it doesn't cover the question, say so "
        "plainly and give general, cautious guidance. Keep answers short, "
        "clear, and practical - 2 to 5 sentences unless the question "
        "needs a list. No markdown symbols, no emojis."
    )
    user_prompt = f"Reference material:\n{context}\n\nQuestion: {question_en}{hist_ctx}"
    resp = _groq_client.chat.completions.create(
        model=CHAT_MODEL, max_tokens=400, temperature=0.3,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return resp.choices[0].message.content.strip()


def _answer_from_web(question_en, hist_ctx):
    system_prompt = (
        "You are a helpful voice assistant. Use web search to find "
        "accurate, current information whenever it would improve your "
        "answer, then answer the question directly using what you find. "
        "Keep answers short, clear, and practical in simple language - "
        "2 to 5 sentences unless the question needs a list. "
        "No markdown symbols, no emojis, and don't narrate that you "
        "searched or list sources/links inline - just give the answer "
        "itself, in plain spoken language, since this may be read aloud."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Question: {question_en}{hist_ctx}"},
    ]
    try:
        resp = _groq_client.chat.completions.create(
            model=SEARCH_MODEL, max_tokens=400, temperature=0.3, messages=messages,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[modl] WARNING: web-search model ({SEARCH_MODEL}) failed: {e}; "
              f"falling back to {CHAT_MODEL} without live web search.")
        resp = _groq_client.chat.completions.create(
            model=CHAT_MODEL, max_tokens=400, temperature=0.3, messages=messages,
        )
        return resp.choices[0].message.content.strip()


def answer_query(question_en, history=None, mode=None):
    if not _groq_client:
        return "The assistant is not configured. Please set GROQ_API_KEY."

    mode = (mode or get_answer_mode()).strip().lower()
    if mode not in _VALID_ANSWER_MODES:
        mode = "web"

    hist_ctx = ""
    if history:
        recent = history[-6:]
        hist_ctx = "\n\nRecent conversation:\n" + "\n".join(
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['text'][:200]}"
            for m in recent
        )

    try:
        if mode == "document":
            return _answer_from_document(question_en, hist_ctx)
        return _answer_from_web(question_en, hist_ctx)
    except Exception as e:
        return f"Sorry, something went wrong while generating a response: {e}"


def translate_text(text, target_lang_code):
    """Translates English text into the target language. Bhashini has
    priority (better quality for Indian languages); falls back to the
    Groq chat model on any Bhashini failure or if it isn't configured."""
    target_lang_code = _clamp_lang(target_lang_code)
    if not text or target_lang_code == "en":
        return text

    if bhashini_configured():
        try:
            return bhashini_translate(text, "en", target_lang_code)
        except Exception as e:
            print(f"[modl] Bhashini translate failed ({e}), falling back to Groq.")

    if not _groq_client:
        return text

    lang_name = LANG_NAMES[target_lang_code]
    system_prompt = (
        f"Translate the given English text fully into {lang_name}, using the "
        f"native script for {lang_name}. Translate every sentence - do not "
        f"leave any part of the text in English, including sentences that "
        f"mix in numbers or technical terms; only proper nouns and numerals "
        f"themselves may stay as-is. Reply with only the translation, "
        f"nothing else."
    )
    try:
        resp = _groq_client.chat.completions.create(
            model=CHAT_MODEL,
            max_tokens=1200,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return text


# ---------------------------------------------------------------------------
# Text-to-speech: three tiers, tried in order, each falling back to the
# next automatically on any failure:
#
#   1. Bhashini TTS  - HIGHEST PRIORITY. Best quality for Indian
#                      languages, needs BHASHINI_USER_ID/BHASHINI_ULCA_API_KEY.
#   2. espeak-ng     - fully offline, always available once installed.
#                      Handles the FULL response text - including mixed
#                      scripts and embedded numbers - in the single
#                      correct voice for the detected/target language (see
#                      the "-b 1" note below for the fix that stopped it
#                      from occasionally reading everything in English).
#   3. Groq TTS      - last resort, English only (Groq doesn't currently
#                      offer Hindi/Bengali/Marathi/Telugu/Tamil/Gujarati/
#                      Kannada/Malayalam/Punjabi voices), so it's skipped
#                      automatically for every other supported language.
# ---------------------------------------------------------------------------

ESPEAK_BIN = shutil.which("espeak-ng") or shutil.which("espeak")

ESPEAK_VOICE_MAP = {
    "en": "en-us", "hi": "hi", "bn": "bn", "mr": "mr", "te": "te",
    "ta": "ta", "gu": "gu", "kn": "kn", "ml": "ml", "pa": "pa",
}


def espeak_available():
    return ESPEAK_BIN is not None


def espeak_voice_for_lang(lang_code):
    return ESPEAK_VOICE_MAP.get(_clamp_lang(lang_code), "en-us")


def groq_tts_available():
    return _groq_client is not None


GROQ_TTS_LANGS = {"en"}


def _synthesize_groq(text, lang_code, out_wav_path):
    if not _groq_client:
        raise RuntimeError("GROQ_API_KEY is not configured.")
    if lang_code not in GROQ_TTS_LANGS:
        raise RuntimeError(f"Groq TTS only supports English, not '{lang_code}'.")
    response = _groq_client.audio.speech.create(
        model=GROQ_TTS_MODEL, voice=GROQ_TTS_VOICE, input=text, response_format="wav",
    )
    response.write_to_file(out_wav_path)


def tts_available():
    return bhashini_configured() or espeak_available() or groq_tts_available()


def synthesize_speech(text, lang_code, out_wav_path, speed_wpm=155):
    """Synthesises `text` (the FULL response, whatever mix of scripts and
    numbers it contains) into a WAV file, trying Bhashini -> espeak-ng ->
    Groq TTS in order and falling through to the next on any failure."""
    lang_code = _clamp_lang(lang_code)
    errors = []

    if bhashini_configured():
        try:
            bhashini_tts(text, lang_code, out_wav_path)
            return
        except Exception as e:
            errors.append(f"Bhashini: {e}")
            print(f"[modl] Bhashini TTS failed ({e}), trying espeak-ng next.")

    if espeak_available():
        voice = espeak_voice_for_lang(lang_code)
        print(f"[modl] synthesize_speech: lang={lang_code!r} -> espeak voice={voice!r}")
        fd, txt_path = tempfile.mkstemp(suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            subprocess.run(
                [
                    ESPEAK_BIN,
                    "-b", "1",          # force UTF-8 input interpretation - without this,
                                        # some builds/platforms (notably Windows) guess the
                                        # wrong input encoding for non-Latin scripts, garble
                                        # every non-ASCII character, and end up reading the
                                        # whole thing as if it were English text instead of
                                        # the requested language.
                    "-v", voice,
                    "-s", str(speed_wpm),
                    "-f", txt_path,
                    "-w", out_wav_path,
                ],
                check=True, capture_output=True, timeout=30,
            )
            return
        except Exception as e:
            errors.append(f"espeak-ng: {e}")
            print(f"[modl] espeak-ng failed ({e}), trying Groq TTS next.")
        finally:
            try:
                os.remove(txt_path)
            except OSError:
                pass

    if lang_code in GROQ_TTS_LANGS and groq_tts_available():
        try:
            _synthesize_groq(text, lang_code, out_wav_path)
            return
        except Exception as e:
            errors.append(f"Groq TTS: {e}")
    elif lang_code not in GROQ_TTS_LANGS:
        print(f"[modl] skipping Groq TTS for '{lang_code}' - it only supports English.")

    raise RuntimeError(
        "No text-to-speech backend could produce audio. Tried: "
        + ("; ".join(errors) if errors else "nothing (none configured/installed)")
        + ". Configure Bhashini (BHASHINI_USER_ID/BHASHINI_ULCA_API_KEY), "
        "install espeak-ng, or set GROQ_API_KEY (English only) - see README.md."
    )
