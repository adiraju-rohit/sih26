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

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_ON_VERCEL = bool(os.environ.get("VERCEL"))

# On Vercel, EVERY path except /tmp is read-only, and that's not
# durable storage anyway (see the "Deploying to Vercel" section of
# README.md) - so on Vercel we always force the database into /tmp,
# full stop, regardless of what DB_PATH is set to. This matters because
# it's easy to end up with a non-empty DB_PATH env var on Vercel without
# meaning to (e.g. copying the local .env's "DB_PATH=app.db" straight
# into the Vercel dashboard's Environment Variables). Previously that
# silently overrode the safe /tmp fallback below, sqlite3.connect() then
# tried to open a file on the read-only filesystem, and the whole
# function crashed on every single request (including the ESP32's
# /esp/answer, which is why nothing ever came out of the speaker or
# showed up on the web page either - the request never completed).
if _ON_VERCEL:
    DB_PATH = os.path.join(tempfile.gettempdir(), "app.db")
else:
    DB_PATH = os.environ.get("DB_PATH", "") or "app.db"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# Resolved relative to this file (not the process's current working
# directory) so it works the same locally and on Vercel, where the cwd a
# serverless function starts in isn't guaranteed to be the project root.
KNOWLEDGE_FILE = os.environ.get("KNOWLEDGE_FILE") or os.path.join(_BASE_DIR, "data", "knowledge.txt")
if not os.path.isabs(KNOWLEDGE_FILE):
    KNOWLEDGE_FILE = os.path.join(_BASE_DIR, KNOWLEDGE_FILE)

# Current (non-deprecated) Groq model ids.
# llama-3.1-8b-instant / llama-3.3-70b-versatile were retired by Groq -
# gpt-oss-120b is the recommended general purpose replacement, whisper-large-v3
# is used for speech recognition and translation.
CHAT_MODEL = os.environ.get("GROQ_CHAT_MODEL", "openai/gpt-oss-120b")
STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3")

_groq_client = None
if GROQ_API_KEY:
    try:
        from groq import Groq
        _groq_client = Groq(api_key=GROQ_API_KEY)
    except Exception as e:
        # Never let a bad/missing key, a network hiccup during client
        # construction, or an incompatible groq-sdk version take down the
        # entire app at import time - fall back to "not configured" and
        # let answer_query()/transcribe_and_translate() report the error
        # per-request instead.
        print(f"[modl] WARNING: failed to initialize Groq client: {e}")
        _groq_client = None

# Map ISO-639-1 codes returned by Whisper to BCP-47 tags usable by the
# browser's speechSynthesis API for text-to-speech output. Only languages
# where the plain 2-letter code isn't a good enough tag on its own (or
# where we want a specific region) need an entry here - see
# bcp47_for_lang() below, which falls back to the bare code for anything
# not listed, so *any* language Whisper detects gets a usable tag instead
# of silently defaulting to English.
LANG_TO_BCP47 = {
    "en": "en-IN", "hi": "hi-IN", "te": "te-IN", "ta": "ta-IN", "kn": "kn-IN",
    "ml": "ml-IN", "mr": "mr-IN", "bn": "bn-IN", "gu": "gu-IN", "pa": "pa-IN",
    "ur": "ur-IN", "or": "or-IN", "as": "as-IN",
}


def bcp47_for_lang(lang_code):
    """Returns a BCP-47 tag for the browser's speechSynthesis API for any
    language code Whisper can return - not just the handful of Indian
    languages this project was originally built around. Falls back to the
    bare 2-letter code (e.g. "fr", "ja", "de") when we don't have a more
    specific region tag on file; browsers match that to whatever voice
    they have installed for that language, so speech still comes out in
    the right language even without a curated entry."""
    code = (lang_code or "en")[:2].lower()
    return LANG_TO_BCP47.get(code, code)


# Used to build a natural-language instruction for the Groq translation
# step ("Translate into {name}"). Not exhaustive - for a code not listed
# here we just pass the raw code through, which current Groq chat models
# still understand fine (e.g. "Translate into ja").
LANG_NAMES = {
    "en": "English", "hi": "Hindi", "te": "Telugu", "ta": "Tamil", "kn": "Kannada",
    "ml": "Malayalam", "mr": "Marathi", "bn": "Bengali", "gu": "Gujarati",
    "pa": "Punjabi", "ur": "Urdu", "or": "Odia", "as": "Assamese",
    "fr": "French", "de": "German", "es": "Spanish", "pt": "Portuguese",
    "it": "Italian", "nl": "Dutch", "ru": "Russian", "zh": "Chinese",
    "ja": "Japanese", "ko": "Korean", "ar": "Arabic", "tr": "Turkish",
    "vi": "Vietnamese", "th": "Thai", "id": "Indonesian", "ms": "Malay",
    "fa": "Persian", "pl": "Polish", "uk": "Ukrainian", "sw": "Swahili",
    "ne": "Nepali", "si": "Sinhala", "my": "Burmese", "he": "Hebrew",
    "el": "Greek", "sv": "Swedish", "fi": "Finnish", "no": "Norwegian",
    "da": "Danish", "cs": "Czech", "ro": "Romanian", "hu": "Hungarian",
}

# ---------------------------------------------------------------------------
# espeak-ng voices - built dynamically from `espeak-ng --voices` so the
# ESP32 device can be spoken to in essentially any language espeak-ng
# ships a voice for (100+), not just a hand-picked shortlist. A small
# curated override map is kept below for cases where we want a specific
# variant (e.g. American rather than Caribbean/British English).
# ---------------------------------------------------------------------------

ESPEAK_BIN = shutil.which("espeak-ng") or shutil.which("espeak")

# Curated overrides: checked before the dynamically-discovered voice list,
# so these specific variants always win regardless of what espeak-ng's
# "Other Languages" priority table would otherwise pick.
ESPEAK_VOICE_MAP = {
    "en": "en-us",
}


def _load_espeak_voice_map():
    """Parses `espeak-ng --voices` into {language_code: voice_identifier}
    covering every language and every alias espeak-ng knows about (its
    "Other Languages" column), e.g. "zh" -> the Mandarin voice, "cmn" ->
    the same voice, etc. Returns {} if espeak-ng isn't installed or the
    output can't be parsed - callers fall back to English in that case."""
    if not ESPEAK_BIN:
        return {}
    try:
        output = subprocess.run(
            [ESPEAK_BIN, "--voices"], capture_output=True, text=True,
            timeout=10, check=True,
        ).stdout
    except Exception:
        return {}

    # best[code] = (priority, voice_identifier); lower priority number =
    # better match, matching espeak-ng's own "Other Languages" ranking.
    best = {}
    for line in output.splitlines()[1:]:
        # Columns: Pty  Language  Age/Gender  VoiceName  File  [Other Languages]
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
        # Fall back to a bare primary-language guess (e.g. register "fr"
        # from "fr-be") only as a weak default - real priorities from the
        # "Other Languages" column below (e.g. "fr-fr" explicitly listing
        # "(fr 5)") should always win over this guess.
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
    """Picks the best espeak-ng voice identifier for a language code,
    checking (in order): the curated override map, an exact match in the
    dynamically discovered voice list, a match on just the primary
    2-letter code, then finally falling back to American English so
    synthesis always produces *something* rather than failing outright."""
    code = (lang_code or "en").strip().lower()
    two_letter = code[:2]

    if code in ESPEAK_VOICE_MAP:
        return ESPEAK_VOICE_MAP[code]
    if two_letter in ESPEAK_VOICE_MAP:
        return ESPEAK_VOICE_MAP[two_letter]
    if code in _ESPEAK_VOICES:
        return _ESPEAK_VOICES[code]
    if two_letter in _ESPEAK_VOICES:
        return _ESPEAK_VOICES[two_letter]
    return "en-us"


# ---------------------------------------------------------------------------
# Database
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
        _ensure_source_column()
    except Exception as e:
        # Don't let a DB problem take the whole app down at import time -
        # every route that touches the DB will still fail (and report why
        # via its own try/except), but at least routes that don't (like
        # /health) keep working, and the crash is visible in the server
        # logs instead of being an opaque "FUNCTION_INVOCATION_FAILED".
        print(f"[modl] WARNING: init_db() failed (DB_PATH={DB_PATH!r}): {e}")


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
            [ESPEAK_BIN, "-v", voice, "-s", str(speed_wpm), "-f", txt_path, "-w", out_wav_path],
            check=True, capture_output=True, timeout=30,
        )
    finally:
        try:
            os.remove(txt_path)
        except OSError:
            pass
