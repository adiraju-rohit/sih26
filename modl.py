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

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_ON_VERCEL = bool(os.environ.get("VERCEL"))

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

# Groq Compound is a system (not a plain chat model) that can decide on its
# own, server-side, to run a real web search (and read the pages it finds)
# before answering - no extra API keys, scraping code, or search service
# of our own required. This is what answer_query() now uses instead of the
# old "read the whole knowledge.txt file" approach. See
# https://console.groq.com/docs/compound for details.
SEARCH_MODEL = os.environ.get("GROQ_SEARCH_MODEL", "groq/compound")

_groq_client = None
if GROQ_API_KEY:
    try:
        from groq import Groq
        _groq_client = Groq(api_key=GROQ_API_KEY)
    except Exception as e:
        print(f"[modl] WARNING: failed to initialize Groq client: {e}")
        _groq_client = None

LANG_TO_BCP47 = {
    "en": "en-IN", "hi": "hi-IN", "te": "te-IN",
}


def bcp47_for_lang(lang_code):
    return LANG_TO_BCP47[_clamp_lang(lang_code)]

LANG_NAMES = {
    "en": "English", "hi": "Hindi", "te": "Telugu",
}

# The system only supports these three languages end-to-end (speech
# recognition, translation, and text-to-speech). Anything else Whisper
# might detect gets clamped to English rather than passed through, so a
# stray/misdetected language never reaches the translator or TTS step with
# an unsupported code.
ALLOWED_LANGS = {"en", "hi", "te"}


def _clamp_lang(lang_code):
    code = (lang_code or "en")[:2].lower()
    return code if code in ALLOWED_LANGS else "en"

ESPEAK_BIN = shutil.which("espeak-ng") or shutil.which("espeak")
# Fixed, known-good espeak-ng voice ids for the three supported languages -
# no need for the dynamic --voices scan below to guess these.
ESPEAK_VOICE_MAP = {
    "en": "en-us",
    "hi": "hi",
    "te": "te",
}


def _load_espeak_voice_map():
    if not ESPEAK_BIN:
        return {}
    try:
        output = subprocess.run(
            [ESPEAK_BIN, "--voices"], capture_output=True, text=True,
            timeout=10, check=True,
        ).stdout
    except Exception:
        return {}

    best = {}
    for line in output.splitlines()[1:]:
        parts = line.split(None, 5)
        if len(parts) < 2:
            continue
        identifier = parts[1].strip()
        if not identifier:
            continue

        def _consider(code, priority):
            code = code.lower()
            if code not in best or priority < best[code][0]:
                best[code] = (priority, identifier)

        _consider(identifier, 0)
        _consider(identifier.split("-")[0], 50)

        if len(parts) == 6:
            for alias_code, alias_priority in re.findall(r"\(([\w-]+)\s+(\d+)\)", parts[5]):
                _consider(alias_code, int(alias_priority))

    return {code: ident for code, (_, ident) in best.items()}


try:
    _ESPEAK_VOICES = _load_espeak_voice_map()
except Exception as e:
    print(f"[modl] WARNING: failed to load espeak-ng voice list: {e}")
    _ESPEAK_VOICES = {}


def espeak_voice_for_lang(lang_code):
    """Returns the espeak-ng voice id for one of the three supported
    languages (English, Hindi, Telugu). Anything else is clamped to
    English first, so this never has to guess at an unsupported voice."""
    code = _clamp_lang(lang_code)
    if code in ESPEAK_VOICE_MAP:
        return ESPEAK_VOICE_MAP[code]
    # Defensive fallback only - shouldn't normally be reached since
    # ESPEAK_VOICE_MAP already covers all three supported codes.
    if code in _ESPEAK_VOICES:
        return _ESPEAK_VOICES[code]
    return "en-us"

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
        _ensure_source_column()
    except Exception as e:
        print(f"[modl] WARNING: init_db() failed (DB_PATH={DB_PATH!r}): {e}")


def _ensure_source_column(_conn_unused=None):
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


try:
    _kb = KnowledgeBase(KNOWLEDGE_FILE)
except Exception as e:
    print(f"[modl] WARNING: failed to load knowledge base from {KNOWLEDGE_FILE!r}: {e}")
    _kb = KnowledgeBase.__new__(KnowledgeBase)
    _kb.path, _kb.chunks, _kb._doc_freq, _kb._chunk_vectors = KNOWLEDGE_FILE, [], Counter(), []


def kb_context(query, top_k=3):
    results = _kb.search(query, top_k=top_k)
    if not results:
        return ""
    return "\n\n".join(results)


# ---------------------------------------------------------------------------
# Local knowledge.txt document (NO LONGER used by answer_query())
#
# This project used to answer questions purely from a local knowledge.txt
# file - first via TF-IDF chunk retrieval (KnowledgeBase.search /
# kb_context), then later by sending the model the entire file as context.
# answer_query() now answers using a live Groq Compound web search instead
# (see below), so neither of those is on the answering path any more.
# Both are left in place, unused, only in case something else in the app
# still wants to read/search that local file directly.
# ---------------------------------------------------------------------------

# Safety cap so an unexpectedly huge knowledge file can't blow past the
# chat model's context window (or make every answer slow/expensive).
# Override via the MAX_KNOWLEDGE_CHARS env var if your file is bigger and
# your model can handle it - gpt-oss-120b's context window comfortably
# fits well beyond this default.
MAX_KNOWLEDGE_CHARS = int(os.environ.get("MAX_KNOWLEDGE_CHARS", "120000"))


def _read_full_knowledge_text():
    if not os.path.exists(KNOWLEDGE_FILE):
        return ""
    try:
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError as e:
        print(f"[modl] WARNING: failed to read knowledge file {KNOWLEDGE_FILE!r}: {e}")
        return ""


_FULL_KNOWLEDGE_TEXT = _read_full_knowledge_text()
if len(_FULL_KNOWLEDGE_TEXT) > MAX_KNOWLEDGE_CHARS:
    print(f"[modl] WARNING: {KNOWLEDGE_FILE} is {len(_FULL_KNOWLEDGE_TEXT)} chars, "
          f"which is over MAX_KNOWLEDGE_CHARS ({MAX_KNOWLEDGE_CHARS}); truncating "
          f"what's sent to the model. Raise MAX_KNOWLEDGE_CHARS if your model's "
          f"context window can fit the whole file.")


def reload_knowledge_base():
    """Re-reads KNOWLEDGE_FILE from disk - call this after editing the
    file without restarting the server. Refreshes both the full-text copy
    answer_query() uses and the TF-IDF index kept around for kb_context()."""
    global _FULL_KNOWLEDGE_TEXT
    _FULL_KNOWLEDGE_TEXT = _read_full_knowledge_text()
    _kb.reload()


# ---------------------------------------------------------------------------
# Groq: speech-to-text + translation to English
# ---------------------------------------------------------------------------

def transcribe_and_translate(audio_path):
    """Returns dict: {text_original, lang, text_english}.
    Uses Groq Whisper for transcription (detects source language) and the
    Whisper translation endpoint (always outputs English) as the
    replacement for the unavailable Bhashini speech-to-text + translation
    pipeline.

    Only English, Hindi, and Telugu are supported. Whisper is constrained
    to guess one of these three up front via the `language` hint isn't
    possible (we don't know which of the three was spoken until we ask),
    so instead we let it auto-detect freely and then clamp the result:
    anything Whisper reports outside {en, hi, te} is treated as English
    for every step after this (translation target, TTS voice, OLED
    language tag), so an occasional misdetection on background noise or
    an unsupported language never breaks translation or speech synthesis.
    """
    if not _groq_client:
        raise RuntimeError("GROQ_API_KEY is not configured.")

    with open(audio_path, "rb") as f:
        transcription = _groq_client.audio.transcriptions.create(
            file=f,
            model=STT_MODEL,
            response_format="verbose_json",
        )
    text_original = (transcription.text or "").strip()
    detected_lang_raw = getattr(transcription, "language", "en") or "en"
    detected_lang = _clamp_lang(detected_lang_raw)

    if detected_lang_raw != detected_lang:
        print(f"[modl] transcribe: Whisper detected {detected_lang_raw!r}, "
              f"which isn't one of the supported languages {sorted(ALLOWED_LANGS)} "
              f"- treating as English.")

    if detected_lang == "en":
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
# Groq: answer generation grounded in a live web search
#
# This used to send the model the entire contents of a local
# knowledge.txt file as context. It now instead calls "groq/compound" -
# not a plain chat model but a Groq *system* that can decide on its own to
# run a real web search (and read the pages it finds) server-side before
# answering, then folds what it found into a normal chat-completion
# response. No local document, search API key, or scraping code needed on
# our end - Groq handles the search, page-reading, and citation-gathering
# entirely on its side. See https://console.groq.com/docs/compound.
# ---------------------------------------------------------------------------

def answer_query(question_en, history=None):
    """Answers an English question by letting Groq Compound search the web
    for whatever current, accurate information it needs, instead of
    looking one up in a local file. Falls back to a plain (non-searching)
    chat completion on CHAT_MODEL if the search-capable model call fails
    for any reason (e.g. it's briefly unavailable, or not enabled on the
    account), so the assistant still answers - just without live search
    that one time."""
    if not _groq_client:
        return "The assistant is not configured. Please set GROQ_API_KEY."

    system_prompt = (
        "You are a helpful voice assistant. Use web search to find "
        "accurate, current information only about cooperative laws, government schemes, PACS services, crop insurance schemes, financial literacy, grievance redressal mechanisms, cooperative governance, legal provisions, member services, and related things for rural farmers and stakeholders in India whenever it would improve your "
        "answer - for facts, prices, schedules, news, or anything that "
        "could have changed or that you're not certain about - then "
        "answer the question directly using what you find. "
        "Keep answers short, clear, and practical in simple language - "
        "2 to 5 sentences unless the question needs a list. "
        "No markdown symbols, no emojis, and don't narrate that you "
        "searched or list sources/links inline - just give the answer "
        "itself, in plain spoken language, since this may be read aloud."
    )

    hist_ctx = ""
    if history:
        recent = history[-6:]
        hist_ctx = "\n\nRecent conversation:\n" + "\n".join(
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['text'][:200]}"
            for m in recent
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Question: {question_en}{hist_ctx}"},
    ]

    try:
        resp = _groq_client.chat.completions.create(
            model=SEARCH_MODEL,
            max_tokens=400,
            temperature=0.3,
            messages=messages,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[modl] WARNING: web-search model ({SEARCH_MODEL}) failed: {e}; "
              f"falling back to {CHAT_MODEL} without live web search.")
        try:
            resp = _groq_client.chat.completions.create(
                model=CHAT_MODEL,
                max_tokens=400,
                temperature=0.3,
                messages=messages,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e2:
            return f"Sorry, something went wrong while generating a response: {e2}"


def translate_text(text, target_lang_code):
    """Translates English text into the target language using the chat
    model. Only Hindi and Telugu are supported as translation targets -
    anything else (including plain "en") is returned unchanged."""
    target_lang_code = _clamp_lang(target_lang_code)
    if target_lang_code == "en" or not _groq_client:
        return text

    lang_name = LANG_NAMES[target_lang_code]
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


def espeak_language_count():
    """Number of distinct language codes espeak-ng can currently speak
    (including aliases) - surfaced on /esp/health mainly so it's easy to
    confirm at a glance that the dynamic voice list actually loaded."""
    return len(_ESPEAK_VOICES)


def synthesize_speech(text, lang_code, out_wav_path, speed_wpm=155):
    """Synthesises `text` into a 16-bit PCM WAV file at `out_wav_path` using
    espeak-ng, in whatever language `lang_code` is (any language espeak-ng
    ships a voice for - see espeak_voice_for_lang() above). Raises
    RuntimeError if espeak-ng is not installed on the server, or
    CalledProcessError if synthesis fails."""
    if not ESPEAK_BIN:
        raise RuntimeError(
            "espeak-ng is not installed on the server. Install it with "
            "'sudo apt-get install espeak-ng' (Debian/Ubuntu) or the "
            "equivalent for your OS."
        )
    voice = espeak_voice_for_lang(lang_code)
    print(f"[modl] synthesize_speech: lang={lang_code!r} -> espeak voice={voice!r}")

    # Write text to a temp file rather than passing it as a CLI argument, to
    # avoid shell-escaping and encoding issues with non-Latin scripts.
    fd, txt_path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        subprocess.run(
            [ESPEAK_BIN, "-a", "200", "-v", voice, "-s", str(speed_wpm), "-f", txt_path, "-w", out_wav_path],
            check=True, capture_output=True, timeout=30,
        )
    finally:
        try:
            os.remove(txt_path)
        except OSError:
            pass
