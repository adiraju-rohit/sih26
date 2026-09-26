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


@app.route("/api/text_message", methods=["POST"])
def api_text_message():
    """Typed-question chat, always in English."""
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message."}), 400

    modl.add_message(CHAT_USER, "user", text, "en", source="web")

    try:
        history = modl.get_messages(CHAT_USER)
        answer_en = modl.answer_query(text, history)
    except Exception:
        app.logger.exception("api_text_message: failed generating an answer")
        answer_en = "Sorry, something went wrong while generating a response. Please try again in a moment."

    last_id = modl.add_message(CHAT_USER, "assistant", answer_en, "en", source="web")
    audio_url = _synthesize_for_web(answer_en, "en")
    return jsonify({
        "ok": True, "user_text": text, "lang": "en", "answer": answer_en,
        "speech_lang": "en-IN", "audio_url": audio_url, "last_id": last_id,
    })


@app.route("/api/voice_message", methods=["POST"])
def api_voice_message():
    """Recorded voice clip from the browser mic: speech-to-text +
    translation to English, an answer, then translation back for
    text-to-speech playback on the client."""
    audio = request.files.get("audio")
    if not audio:
        return jsonify({"ok": False, "error": "No audio received."}), 400

    filename = f"{uuid.uuid4()}.webm"
    filepath = os.path.join(UPLOAD_DIR, filename)
    audio.save(filepath)

    try:
        stt = modl.transcribe_and_translate(filepath)
    except Exception as e:
        app.logger.exception("api_voice_message: speech recognition failed")
        return jsonify({"ok": False, "error": f"Speech recognition failed: {e}"}), 500
    finally:
        try:
            os.remove(filepath)
        except OSError:
            pass

    text_original = stt["text_original"]
    lang = stt["lang"]
    text_english = stt["text_english"]
    if not text_original:
        return jsonify({"ok": False, "error": "Could not detect any speech. Please try again."}), 400

    modl.add_message(CHAT_USER, "user", text_original, lang, source="web")

    try:
        history = modl.get_messages(CHAT_USER)
        answer_en = modl.answer_query(text_english, history)
        answer_translated = modl.translate_text(answer_en, lang)
    except Exception:
        app.logger.exception("api_voice_message: failed generating an answer")
        answer_translated = modl.translate_text(
            "Sorry, something went wrong while generating a response. Please try again in a moment.", lang)

    last_id = modl.add_message(CHAT_USER, "assistant", answer_translated, lang, source="web")
    speech_lang = modl.bcp47_for_lang(lang)
    audio_url = _synthesize_for_web(answer_translated, lang)

    return jsonify({
        "ok": True, "user_text": text_original, "lang": lang, "answer": answer_translated,
        "speech_lang": speech_lang, "audio_url": audio_url, "last_id": last_id,
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


def _cleanup_esp_audio(max_age_seconds=600):
    now = time.time()
    try:
        for fname in os.listdir(ESP_AUDIO_DIR):
            fpath = os.path.join(ESP_AUDIO_DIR, fname)
            if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > max_age_seconds:
                os.remove(fpath)
    except OSError:
        pass


def _cleanup_web_audio(max_age_seconds=600):
    now = time.time()
    try:
        for fname in os.listdir(WEB_AUDIO_DIR):
            fpath = os.path.join(WEB_AUDIO_DIR, fname)
            if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > max_age_seconds:
                os.remove(fpath)
    except OSError:
        pass


def _synthesize_for_web(text, lang):
    """Generates a WAV file for the browser to play, using the same
    Bhashini -> espeak-ng -> Groq TTS priority chain as the ESP32 device,
    instead of leaving audio to the browser's own speechSynthesis. This
    is what guarantees the reply is actually spoken in the input
    language: most browsers/OSes simply don't ship an installed voice for
    languages like Telugu, Tamil, Bengali, etc., so speechSynthesis
    silently falls back to whatever default voice is installed (usually
    English) - it plays *something*, just not in the right language.
    Generating the audio ourselves sidesteps that entirely. Returns a URL
    path the browser can GET, or None if synthesis isn't available/fails
    (the browser then falls back to speechSynthesis - fine for English,
    better than nothing for other languages)."""
    if not modl.tts_available():
        return None
    _cleanup_web_audio()
    audio_id = f"{uuid.uuid4()}.wav"
    audio_path = os.path.join(WEB_AUDIO_DIR, audio_id)
    try:
        modl.synthesize_speech(text, lang, audio_path)
        return f"/audio/{audio_id}"
    except Exception:
        app.logger.exception("_synthesize_for_web: speech synthesis failed")
        return None


@app.route("/audio/<filename>")
def web_audio(filename):
    safe_name = os.path.basename(filename)
    filepath = os.path.join(WEB_AUDIO_DIR, safe_name)
    if not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, mimetype="audio/wav", conditional=False)


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
    the device."""
    if not _check_esp_auth():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    try:
        sample_rate = int(request.headers.get("X-Sample-Rate", "16000"))
    except ValueError:
        sample_rate = 16000

    filename = f"{uuid.uuid4()}.wav"
    filepath = os.path.join(UPLOAD_DIR, filename)
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
        try:
            os.remove(filepath)
        except OSError:
            pass
        return jsonify({"ok": False, "error": f"Upload failed: {e}"}), 500

    if total_bytes < 100:
        try:
            os.remove(filepath)
        except OSError:
            pass
        return jsonify({"ok": False, "error": "No audio received."}), 400

    try:
        stt = modl.transcribe_and_translate(filepath)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Speech recognition failed: {e}"}), 500
    finally:
        try:
            os.remove(filepath)
        except OSError:
            pass

    if not stt["text_original"]:
        return jsonify({"ok": False, "error": "No speech detected."}), 400

    modl.add_message(DEVICE_USER, "user", stt["text_original"], stt["lang"], source="esp32")
    return jsonify({"ok": True, "text_original": stt["text_original"],
                     "text_english": stt["text_english"], "lang": stt["lang"]})


@app.route("/esp/answer", methods=["POST"])
def esp_answer():
    """Body: JSON {text_english, lang}. Generates an answer and, separately,
    tries to speak it - a TTS failure never hides the text answer."""
    if not _check_esp_auth():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    text_english = (data.get("text_english") or "").strip()
    lang = (data.get("lang") or "en").strip()
    if not text_english:
        return jsonify({"ok": False, "error": "Missing text_english."}), 400

    try:
        history = modl.get_messages(DEVICE_USER)
        answer_en = modl.answer_query(text_english, history)
        answer_translated = modl.translate_text(answer_en, lang)
    except Exception as e:
        app.logger.exception("esp_answer: failed generating an answer")
        return jsonify({"ok": False, "error": f"Failed to generate an answer: {e}"}), 500

    modl.add_message(DEVICE_USER, "assistant", answer_translated, lang, source="esp32")

    audio_url, tts_error = None, None
    if not modl.tts_available():
        tts_error = "No text-to-speech backend is configured - see README.md. The text answer was still generated and saved."
        app.logger.warning("esp_answer: %s", tts_error)
    else:
        _cleanup_esp_audio()
        audio_id = f"{uuid.uuid4()}.wav"
        audio_path = os.path.join(ESP_AUDIO_DIR, audio_id)
        try:
            modl.synthesize_speech(answer_translated, lang, audio_path)
            audio_url = f"/esp/audio/{audio_id}"
        except Exception as e:
            tts_error = f"Speech synthesis failed: {e}"
            app.logger.exception("esp_answer: speech synthesis failed")

    return jsonify({"ok": True, "answer_text": answer_translated, "audio_url": audio_url, "tts_error": tts_error})


@app.route("/esp/audio/<filename>")
def esp_audio(filename):
    if not _check_esp_auth():
        abort(401)
    safe_name = os.path.basename(filename)
    filepath = os.path.join(ESP_AUDIO_DIR, safe_name)
    if not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, mimetype="audio/wav", conditional=False)


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_mode, use_reloader=debug_mode, host="0.0.0.0", port=5000, threaded=True)
