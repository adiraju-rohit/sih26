import os
import time
import uuid
import tempfile

from flask import Flask, render_template, request, jsonify, send_file, abort
from dotenv import load_dotenv

import modl

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "26088-dev-secret")

# Vercel's serverless functions can only write to /tmp.
_ON_VERCEL = bool(os.environ.get("VERCEL"))
_BASE_DIR = tempfile.gettempdir() if _ON_VERCEL else "."
UPLOAD_DIR = os.path.join(_BASE_DIR, "uploads")
ESP_AUDIO_DIR = os.path.join(_BASE_DIR, "esp_audio")
WEB_AUDIO_DIR = os.path.join(_BASE_DIR, "web_audio")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(ESP_AUDIO_DIR, exist_ok=True)
os.makedirs(WEB_AUDIO_DIR, exist_ok=True)

# Optional shared secret for the ESP32 device. Leave ESP_API_KEY unset in
# .env to disable this check (fine for a closed hackathon Wi-Fi network).
ESP_API_KEY = os.environ.get("ESP_API_KEY", "")

# No login system - the browser chat and the ESP32 device each get one
# fixed, shared conversation (kept separate from each other so a question
# asked on the physical device doesn't clutter the browser chat, and
# vice versa).
CHAT_USER = "shared"
DEVICE_USER = "esp32-device"

modl.init_db()


@app.route("/health")
def health():
    """Lightweight, unauthenticated reachability check. The ESP32 polls
    this every few seconds while idle so its OLED can show whether the
    server is actually up."""
    return jsonify({"ok": True, "service": "26088", "time": int(time.time())})


@app.route("/")
def root():
    messages = modl.get_messages(CHAT_USER)
    return render_template("chat.html", messages=messages)


@app.route("/api/clear", methods=["POST"])
def api_clear():
    modl.clear_messages(CHAT_USER)
    return jsonify({"ok": True})


@app.route("/api/settings/answer_mode", methods=["GET", "POST"])
def api_answer_mode():
    """Toggles how answer_query() answers questions:
    - "web": Groq (Compound) searches the live web.
    - "document": Groq answers only from the local knowledge.txt file.
    GET returns the current mode; POST {"mode": "web"|"document"} switches
    it immediately, for both the browser and the ESP32 device - no
    restart needed."""
    if request.method == "GET":
        return jsonify({"ok": True, "mode": modl.get_answer_mode()})
    data = request.get_json(force=True, silent=True) or {}
    try:
        new_mode = modl.set_answer_mode(data.get("mode"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "mode": new_mode})


_SORRY = "Sorry, something went wrong while generating a response. Please try again in a moment."


def _recent_history(username, drop_last_user=False):
    """The last few messages, which is all the answer prompt ever uses (it
    used to load 200 rows to keep 6). With drop_last_user, the question that
    was just saved is left out so it isn't sent to the model twice."""
    msgs = modl.get_messages(username, limit=7 if drop_last_user else 6)
    if drop_last_user and msgs and msgs[-1]["role"] == "user":
        msgs = msgs[:-1]
    return msgs


def _reply(text, lang, history):
    """Answer `text` (in `lang`) and return the reply in the same language.
    Translation to English happens inside modl.respond(), only for the
    answer step - the user's own text is stored and shown exactly as given."""
    try:
        return modl.respond(text, lang, history)
    except Exception:
        app.logger.exception("failed generating an answer")
        return modl.translate_text(_SORRY, lang)


@app.route("/api/text_message", methods=["POST"])
def api_text_message():
    """Typed question. The language is detected from the script, so a
    question typed in Telugu gets a Telugu answer; the text is shown and
    stored exactly as typed."""
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message."}), 400

    lang = modl.detect_text_language(text)
    history = _recent_history(CHAT_USER)
    modl.add_message(CHAT_USER, "user", text, lang, source="web")

    answer = _reply(text, lang, history)
    last_id = modl.add_message(CHAT_USER, "assistant", answer, lang, source="web")
    return jsonify({
        "ok": True, "user_text": text, "lang": lang, "answer": answer,
        "speech_lang": modl.bcp47_for_lang(lang),
        "audio_url": _synthesize_for_web(answer, lang), "last_id": last_id,
    })


@app.route("/api/voice_message", methods=["POST"])
def api_voice_message():
    """Recorded voice clip from the browser mic. The transcript is kept in
    the spoken language and is what gets displayed and stored."""
    audio = request.files.get("audio")
    if not audio:
        return jsonify({"ok": False, "error": "No audio received."}), 400

    ext = ".wav" if (audio.filename or "").lower().endswith(".wav") else ".webm"
    filepath = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}{ext}")
    audio.save(filepath)

    try:
        stt = modl.transcribe(filepath)
    except Exception as e:
        app.logger.exception("api_voice_message: speech recognition failed")
        return jsonify({"ok": False, "error": f"Speech recognition failed: {e}"}), 500
    finally:
        _remove_quietly(filepath)

    text, lang = stt["text"], stt["lang"]
    if not text:
        return jsonify({"ok": False, "error": "Could not detect any speech. Please try again."}), 400

    history = _recent_history(CHAT_USER)
    modl.add_message(CHAT_USER, "user", text, lang, source="web")

    answer = _reply(text, lang, history)
    last_id = modl.add_message(CHAT_USER, "assistant", answer, lang, source="web")
    return jsonify({
        "ok": True, "user_text": text, "lang": lang, "answer": answer,
        "speech_lang": modl.bcp47_for_lang(lang),
        "audio_url": _synthesize_for_web(answer, lang), "last_id": last_id,
    })


# ---------------------------------------------------------------------------
# ESP32 device endpoints - unauthenticated (besides the optional
# X-Api-Key), stored under the separate fixed DEVICE_USER conversation.
# ---------------------------------------------------------------------------

def _check_esp_auth():
    if not ESP_API_KEY:
        return True
    return request.headers.get("X-Api-Key", "") == ESP_API_KEY


@app.route("/esp/health", methods=["GET"])
def esp_health():
    if not _check_esp_auth():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    return jsonify({
        "ok": True,
        "groq_configured": bool(modl.GROQ_API_KEY),
        "answer_mode": modl.get_answer_mode(),
        "stt_available": modl.stt_available(),
        "bhashini_configured": modl.bhashini_configured(),
        "espeak_available": modl.espeak_available(),
        "groq_tts_available": modl.groq_tts_available(),
        "tts_available": modl.tts_available(),
    })


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


_last_cleanup = {}


def _cleanup_dir(directory, max_age_seconds=600):
    """Deletes old generated audio. Runs at most once a minute per folder
    instead of listing the folder on every single request."""
    now = time.time()
    if now - _last_cleanup.get(directory, 0) < 60:
        return
    _last_cleanup[directory] = now
    try:
        for fname in os.listdir(directory):
            fpath = os.path.join(directory, fname)
            if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > max_age_seconds:
                os.remove(fpath)
    except OSError:
        pass


def _synthesize_to(directory, url_prefix, text, lang):
    """Speaks `text` into a new WAV in `directory` (Bhashini -> espeak-ng ->
    Groq TTS, see modl.synthesize_speech). Returns (url, error)."""
    _cleanup_dir(directory)
    audio_id = f"{uuid.uuid4()}.wav"
    try:
        modl.synthesize_speech(text, lang, os.path.join(directory, audio_id))
        return f"{url_prefix}/{audio_id}", None
    except Exception as e:
        app.logger.exception("speech synthesis failed")
        return None, f"Speech synthesis failed: {e}"


def _synthesize_for_web(text, lang):
    """URL of server-generated audio in the reply language, or None (the
    browser then falls back to speechSynthesis). Generating the audio here
    is what guarantees e.g. Telugu is actually spoken in Telugu - most
    browsers have no installed voice for it and silently use English."""
    if not modl.tts_available():
        return None
    return _synthesize_to(WEB_AUDIO_DIR, "/audio", text, lang)[0]


def _send_wav(directory, filename):
    filepath = os.path.join(directory, os.path.basename(filename))
    if not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, mimetype="audio/wav", conditional=False)


@app.route("/audio/<filename>")
def web_audio(filename):
    return _send_wav(WEB_AUDIO_DIR, filename)


def _write_wav_header(f, data_bytes, sample_rate, bits_per_sample, num_channels):
    byte_rate = sample_rate * num_channels * (bits_per_sample // 8)
    block_align = num_channels * (bits_per_sample // 8)
    f.write(b"RIFF")
    f.write((36 + data_bytes).to_bytes(4, "little"))
    f.write(b"WAVE")
    f.write(b"fmt ")
    f.write((16).to_bytes(4, "little"))
    f.write((1).to_bytes(2, "little"))
    f.write(num_channels.to_bytes(2, "little"))
    f.write(sample_rate.to_bytes(4, "little"))
    f.write(byte_rate.to_bytes(4, "little"))
    f.write(block_align.to_bytes(2, "little"))
    f.write(bits_per_sample.to_bytes(2, "little"))
    f.write(b"data")
    f.write(data_bytes.to_bytes(4, "little"))


ESP_MAX_UPLOAD_BYTES = 16000 * 2 * 60  # ~60s at 16kHz/16-bit mono


@app.route("/esp/transcribe", methods=["POST"])
def esp_transcribe():
    """Body: raw 16-bit PCM mono audio, streamed chunk-by-chunk by the
    ESP32 while the button is held - never buffered as a whole file on
    the device. `text_original` is exactly what was spoken; `text_english`
    is included only because the device sends it back to /esp/answer."""
    if not _check_esp_auth():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    try:
        sample_rate = int(request.headers.get("X-Sample-Rate", "16000"))
    except ValueError:
        sample_rate = 16000

    filepath = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}.wav")
    total_bytes = 0
    try:
        with open(filepath, "wb") as f:
            f.write(b"\x00" * 44)
            stream = request.stream
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                f.write(chunk)
                total_bytes += len(chunk)
                if total_bytes >= ESP_MAX_UPLOAD_BYTES:
                    break
            f.seek(0)
            _write_wav_header(f, total_bytes, sample_rate, 16, 1)
    except Exception as e:
        _remove_quietly(filepath)
        return jsonify({"ok": False, "error": f"Upload failed: {e}"}), 500

    if total_bytes < 100:
        _remove_quietly(filepath)
        return jsonify({"ok": False, "error": "No audio received."}), 400

    try:
        stt = modl.transcribe_and_translate(filepath)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Speech recognition failed: {e}"}), 500
    finally:
        _remove_quietly(filepath)

    if not stt["text_original"]:
        return jsonify({"ok": False, "error": "No speech detected."}), 400

    modl.add_message(DEVICE_USER, "user", stt["text_original"], stt["lang"], source="esp32")
    return jsonify({"ok": True, "text_original": stt["text_original"],
                    "text_english": stt["text_english"], "lang": stt["lang"]})


@app.route("/esp/answer", methods=["POST"])
def esp_answer():
    """Body: JSON {text_english, lang} (text_original is optional and is
    only used if text_english is missing). Generates an answer and,
    separately, tries to speak it - a TTS failure never hides the text."""
    if not _check_esp_auth():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    text_english = (data.get("text_english") or "").strip()
    text_original = (data.get("text_original") or "").strip()
    lang = (data.get("lang") or "en").strip()
    if not (text_english or text_original):
        return jsonify({"ok": False, "error": "Missing text_english."}), 400

    try:
        history = _recent_history(DEVICE_USER, drop_last_user=True)
        answer = modl.respond(text_original or text_english, lang, history,
                              text_english=text_english or None)
    except Exception as e:
        app.logger.exception("esp_answer: failed generating an answer")
        return jsonify({"ok": False, "error": f"Failed to generate an answer: {e}"}), 500

    modl.add_message(DEVICE_USER, "assistant", answer, lang, source="esp32")

    audio_url, tts_error = None, None
    if not modl.tts_available():
        tts_error = "No text-to-speech backend is configured - see README.md. The text answer was still generated and saved."
        app.logger.warning("esp_answer: %s", tts_error)
    else:
        audio_url, tts_error = _synthesize_to(ESP_AUDIO_DIR, "/esp/audio", answer, lang)

    return jsonify({"ok": True, "answer_text": answer, "audio_url": audio_url, "tts_error": tts_error})


@app.route("/esp/audio/<filename>")
def esp_audio(filename):
    if not _check_esp_auth():
        abort(401)
    return _send_wav(ESP_AUDIO_DIR, filename)


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_mode, use_reloader=debug_mode, host="0.0.0.0", port=5000, threaded=True)
