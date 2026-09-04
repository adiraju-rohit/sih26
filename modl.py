"""
26088 - backend logic module
Handles: user accounts (sqlite), knowledge base retrieval (pure-python TF-IDF,
no external vector DB needed), and all Groq API calls (speech-to-text,
speech translation, chat answer generation, and answer translation).
"""

import os
import re
import time
import math
import shutil
import sqlite3
import tempfile
import subprocess
from collections import Counter

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "app.db")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
KNOWLEDGE_FILE = os.environ.get("KNOWLEDGE_FILE", os.path.join("data", "knowledge.txt"))

# Current (non-deprecated) Groq model ids.
# llama-3.1-8b-instant / llama-3.3-70b-versatile were retired by Groq -
# gpt-oss-120b is the recommended general purpose replacement, whisper-large-v3
# is used for speech recognition and translation.
CHAT_MODEL = os.environ.get("GROQ_CHAT_MODEL", "openai/gpt-oss-120b")
STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3")

_groq_client = None
if GROQ_API_KEY:
    from groq import Groq
    _groq_client = Groq(api_key=GROQ_API_KEY)

# Map ISO-639-1 codes returned by Whisper to BCP-47 tags usable by the
# browser's speechSynthesis API for text-to-speech output.
LANG_TO_BCP47 = {
    "en": "en-IN", "hi": "hi-IN", "te": "te-IN", "ta": "ta-IN", "kn": "kn-IN",
    "ml": "ml-IN", "mr": "mr-IN", "bn": "bn-IN", "gu": "gu-IN", "pa": "pa-IN",
    "ur": "ur-IN", "or": "or-IN", "as": "as-IN",
}
LANG_NAMES = {
    "en": "English", "hi": "Hindi", "te": "Telugu", "ta": "Tamil", "kn": "Kannada",
    "ml": "Malayalam", "mr": "Marathi", "bn": "Bengali", "gu": "Gujarati",
    "pa": "Punjabi", "ur": "Urdu", "or": "Odia", "as": "Assamese",
}

# espeak-ng voice codes used for offline, on-server text-to-speech for the
# ESP32 device (no internet-dependent TTS service required). espeak-ng
# ships with Hindi, Tamil, Telugu, etc. voices out of the box.
ESPEAK_VOICE_MAP = {
    "en": "en-us", "hi": "hi", "ta": "ta", "te": "te", "kn": "kn",
    "ml": "ml", "mr": "mr", "bn": "bn", "gu": "gu", "pa": "pa", "ur": "ur",
}

ESPEAK_BIN = shutil.which("espeak-ng") or shutil.which("espeak")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
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
    _ensure_source_column(conn)


def _ensure_source_column(_conn_unused=None):
    """Adds the 'source' column to pre-existing databases created by an
    older version of this app that didn't have it yet."""
    conn = get_conn()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    if "source" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'web'")
        conn.commit()
    conn.close()


def add_message(username, role, text, lang="en", source="web"):
    conn = get_conn()
    cur = conn.execute("INSERT INTO messages (username, role, text, lang, source, ts) VALUES (?,?,?,?,?,?)",
                        (username, role, text, lang, source, int(time.time())))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def get_messages(username, limit=200):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, role, text, lang, source, ts FROM messages WHERE username=? ORDER BY ts ASC, id ASC LIMIT ?",
        (username, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_messages_since(username, after_id=0, limit=200):
    """Used by the web UI's polling loop to fetch only new messages (e.g.
    ones that just arrived from the ESP32 device) without re-rendering the
    whole conversation."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, role, text, lang, source, ts FROM messages "
        "WHERE username=? AND id>? ORDER BY ts ASC, id ASC LIMIT ?",
        (username, after_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_messages(username):
    conn = get_conn()
    conn.execute("DELETE FROM messages WHERE username=?", (username,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Lightweight retrieval over the knowledge base (pure python TF-IDF, no
# external vector-store dependency required - keeps deployment simple).
# ---------------------------------------------------------------------------

_STOPWORDS = set("""a an the is are was were be been being of to in on for and or
but if then so than that this these those it its as by with from at about into
your you i we our us they he she his her them do does did can could should would
will shall not no yes what when where who whom which how""".split())


def _tokenize(text):
    return [w for w in re.findall(r"[a-zA-Z]+", text.lower()) if w not in _STOPWORDS and len(w) > 2]


class KnowledgeBase:
    """Splits a large text file into topic chunks and answers similarity
    queries using TF-IDF cosine similarity - no external services needed,
    so it works fully offline and scales to large text files."""

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

        # Split on "TOPIC:" markers if present, else on blank lines / fixed size.
        if "TOPIC:" in raw:
            parts = re.split(r"(?=TOPIC:)", raw)
            self.chunks = [p.strip() for p in parts if p.strip()]
        else:
            paras = [p.strip() for p in raw.split("\n\n") if p.strip()]
            self.chunks = []
            buf = ""
            for p in paras:
                if len(buf) + len(p) < 900:
                    buf += ("\n" + p if buf else p)
                else:
                    if buf:
                        self.chunks.append(buf)
                    buf = p
            if buf:
                self.chunks.append(buf)

        # Build TF-IDF vectors.
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


_kb = KnowledgeBase(KNOWLEDGE_FILE)


def kb_context(query, top_k=3):
    results = _kb.search(query, top_k=top_k)
    if not results:
        return ""
    return "\n\n".join(results)


# ---------------------------------------------------------------------------
# Groq: speech-to-text + translation to English
# ---------------------------------------------------------------------------

def transcribe_and_translate(audio_path):
    """Returns dict: {text_original, lang, text_english}.
    Uses Groq Whisper for transcription (detects source language) and the
    Whisper translation endpoint (always outputs English) as the
    replacement for the unavailable Bhashini speech-to-text + translation
    pipeline."""
    if not _groq_client:
        raise RuntimeError("GROQ_API_KEY is not configured.")

    with open(audio_path, "rb") as f:
        transcription = _groq_client.audio.transcriptions.create(
            file=f,
            model=STT_MODEL,
            response_format="verbose_json",
        )
    text_original = (transcription.text or "").strip()
    detected_lang = getattr(transcription, "language", "en") or "en"

    if detected_lang.startswith("en"):
        text_english = text_original
    else:
        with open(audio_path, "rb") as f:
            translation = _groq_client.audio.translations.create(
                file=f,
                model=STT_MODEL,
                response_format="verbose_json",
            )
        text_english = (translation.text or "").strip()

    return {
        "text_original": text_original,
        "lang": detected_lang,
        "text_english": text_english,
    }


# ---------------------------------------------------------------------------
# Groq: answer generation grounded in the knowledge base
# ---------------------------------------------------------------------------

def answer_query(question_en, history=None):
    """Answers an English question using retrieved knowledge-base context."""
    if not _groq_client:
        return "The assistant is not configured. Please set GROQ_API_KEY."

    context = kb_context(question_en, top_k=4)

    system_prompt = (
        "You are a helpful assistant for members of cooperative societies. "
        "Answer questions about cooperative governance, legal provisions, "
        "government schemes, and member services. "
        "Base your answer only on the reference material provided; if the "
        "reference material does not cover the question, say so plainly and "
        "give general, cautious guidance. "
        "Keep answers short, clear, and practical - 2 to 5 sentences unless "
        "the question needs a list. No markdown symbols, no emojis."
    )

    hist_ctx = ""
    if history:
        recent = history[-6:]
        hist_ctx = "\n\nRecent conversation:\n" + "\n".join(
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['text'][:200]}"
            for m in recent
        )

    user_prompt = (
        f"Reference material:\n{context}\n\n"
        f"Question: {question_en}{hist_ctx}"
    )

    try:
        resp = _groq_client.chat.completions.create(
            model=CHAT_MODEL,
            max_tokens=400,
            temperature=0.3,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Sorry, something went wrong while generating a response: {e}"


def translate_text(text, target_lang_code):
    """Translates English text into the target language using the chat model."""
    if target_lang_code.startswith("en") or not _groq_client:
        return text

    lang_name = LANG_NAMES.get(target_lang_code[:2], target_lang_code)
    system_prompt = (
        f"Translate the given English text into {lang_name}, using the native "
        f"script for {lang_name}. Reply with only the translation, nothing else."
    )
    try:
        resp = _groq_client.chat.completions.create(
            model=CHAT_MODEL,
            max_tokens=500,
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
# Offline text-to-speech (espeak-ng) - used for the ESP32 device, which has
# no built-in speech synthesiser and needs a ready-made WAV file to stream
# to its I2S amplifier. This avoids depending on an internet-connected TTS
# service, mirroring the offline-friendly approach used elsewhere in the
# project.
# ---------------------------------------------------------------------------

def espeak_available():
    return ESPEAK_BIN is not None


def synthesize_speech(text, lang_code, out_wav_path, speed_wpm=155):
    """Synthesises `text` into a 16-bit PCM WAV file at `out_wav_path` using
    espeak-ng. Raises RuntimeError if espeak-ng is not installed on the
    server, or CalledProcessError if synthesis fails."""
    if not ESPEAK_BIN:
        raise RuntimeError(
            "espeak-ng is not installed on the server. Install it with "
            "'sudo apt-get install espeak-ng' (Debian/Ubuntu) or the "
            "equivalent for your OS."
        )
    voice = ESPEAK_VOICE_MAP.get((lang_code or "en")[:2], "en-us")

    # Write text to a temp file rather than passing it as a CLI argument, to
    # avoid shell-escaping and encoding issues with non-Latin scripts.
    fd, txt_path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        subprocess.run(
            [ESPEAK_BIN, "-v", voice, "-s", str(speed_wpm), "-f", txt_path, "-w", out_wav_path],
            check=True, capture_output=True, timeout=30,
        )
    finally:
        try:
            os.remove(txt_path)
        except OSError:
            pass
