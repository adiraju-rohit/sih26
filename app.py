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

# Vercel's serverless functions can only write to /tmp - every other path
# is a read-only filesystem, and writing to "uploads"/"esp_audio" (relative
# to the working directory) is exactly what was crashing the function
# there. Detect Vercel (it sets VERCEL=1 automatically) and use /tmp; keep
# plain local folders for normal local/self-hosted runs.
_ON_VERCEL = bool(os.environ.get("VERCEL"))
_BASE_DIR = tempfile.gettempdir() if _ON_VERCEL else "."
UPLOAD_DIR = os.path.join(_BASE_DIR, "uploads")
ESP_AUDIO_DIR = os.path.join(_BASE_DIR, "esp_audio")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(ESP_AUDIO_DIR, exist_ok=True)

# Optional shared secret for the ESP32 device. Leave ESP_API_KEY unset in
# .env to disable this check (fine for a closed hackathon Wi-Fi network).
ESP_API_KEY = os.environ.get("ESP_API_KEY", "")

# There is no login system - the browser chat and the ESP32 device both
# write into the same single conversation, so everything either of them
# says shows up together on both the web page and (as spoken audio) on the
# device.
CHAT_USER = "shared"

modl.init_db()


@app.errorhandler(Exception)
def handle_uncaught_exception(e):
    """Last-resort safety net. Every route below already wraps its own
    Groq/DB/TTS calls in try/except and returns a proper JSON error, but if
    something unexpected still slips through (e.g. a bad request from a
    client, a dependency raising something unforeseen), this makes sure the
    response is still valid JSON the caller (browser JS or the ESP32's
    JSON parser) can handle, instead of the request just crashing silently
    with no visible error - which is what made the "no output at all"
    symptom so hard to see: the request was failing, but nothing surfaced
    that failure anywhere."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        # Preserve normal 404/401/etc responses (e.g. abort(404) in
        # /esp/audio/<filename>) instead of masking them as a 500.
        return e
    app.logger.exception("Unhandled exception")
    return jsonify({"ok": False, "error": f"Internal server error: {e}"}), 500


@app.route("/health")
def health():
    """Lightweight, unauthenticated reachability check. The ESP32 polls
    this every few seconds while idle so its OLED can show whether the
    server is actually up, instead of silently failing on button press."""
    return jsonify({"ok": True, "service": "26088", "time": int(time.time())})


@app.route("/")
def root():
    messages = modl.get_messages(CHAT_USER)
    return render_template("chat.html", messages=messages)


@app.route("/api/clear", methods=["POST"])
def api_clear():
    modl.clear_messages(CHAT_USER)
    return jsonify({"ok": True})


@app.route("/api/messages", methods=["GET"])
def api_messages():
    """Polled by the web page every few seconds so that questions and
    answers coming in from the ESP32 device (asked with the hardware mic,
    not the browser) appear on the chat interface live, without a page
    reload. Pass ?after_id=<last id you already have> to get only new
    messages."""
    try:
        after_id = int(request.args.get("after_id", "0"))
    except ValueError:
        after_id = 0
    messages = modl.get_messages_since(CHAT_USER, after_id=after_id)
    return jsonify({"ok": True, "messages": messages})


@app.route("/api/text_message", methods=["POST"])
def api_text_message():
    """Fallback text-based chat (typed input) for accessibility / testing."""
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message."}), 400

    history = modl.get_messages(CHAT_USER)
    modl.add_message(CHAT_USER, "user", text, "en", source="web")

    answer_en = modl.answer_query(text, history)
    last_id = modl.add_message(CHAT_USER, "assistant", answer_en, "en", source="web")

    return jsonify({
        "ok": True,
        "user_text": text,
        "lang": "en",
        "answer": answer_en,
        "speech_lang": "en-IN",
        "last_id": last_id,
    })


@app.route("/api/voice_message", methods=["POST"])
def api_voice_message():
    """Handles a recorded voice clip from the browser microphone:
    speech-to-text + translation to English, answer generation grounded in
    the knowledge base, and translation of the answer back to the input
    language for text-to-speech playback on the client."""
    audio = request.files.get("audio")
    if not audio:
        return jsonify({"ok": False, "error": "No audio received."}), 400

    filename = f"{uuid.uuid4()}.webm"
    filepath = os.path.join(UPLOAD_DIR, filename)
    audio.save(filepath)

    try:
        stt = modl.transcribe_and_translate(filepath)
    except Exception as e:
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

    history = modl.get_messages(CHAT_USER)
    modl.add_message(CHAT_USER, "user", text_original, lang, source="web")

    answer_en = modl.answer_query(text_english, history)
    answer_translated = modl.translate_text(answer_en, lang)

    last_id = modl.add_message(CHAT_USER, "assistant", answer_translated, lang, source="web")

    speech_lang = modl.bcp47_for_lang(lang)

    return jsonify({
        "ok": True,
        "user_text": text_original,
        "lang": lang,
        "answer": answer_translated,
        "speech_lang": speech_lang,
        "last_id": last_id,
    })


# ---------------------------------------------------------------------------
# ESP32 device endpoints
#
# The device flow is split into three round trips so that each one lines up
# with a distinct status word on its OLED screen:
#   1. POST /esp/transcribe  -> "Translating"  (speech recognition + translate to English)
#   2. POST /esp/answer      -> "Searching"    (knowledge lookup + answer + speech synthesis)
#   3. GET  /esp/audio/<id>  -> "Speaking"     (streamed to the I2S amplifier)
# "Listening" happens entirely on the device itself while the button is held.
#
# Both endpoints write into the SAME shared conversation as the browser
# chat (CHAT_USER), so every question asked on the physical device - and
# its spoken answer - also appears as text on the web interface.
# ---------------------------------------------------------------------------

def _check_esp_auth():
    if not ESP_API_KEY:
        return True
    return request.headers.get("X-Api-Key", "") == ESP_API_KEY


@app.route("/esp/health", methods=["GET"])
def esp_health():
    """Cheap endpoint the device polls periodically so it can show a live
    'server reachable' indicator on the OLED, separate from WiFi being up."""
    if not _check_esp_auth():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    return jsonify({"ok": True, "groq_configured": bool(modl.GROQ_API_KEY),
                     "tts_available": modl.espeak_available(),
                     "tts_languages": modl.espeak_language_count()})


def _cleanup_esp_audio(max_age_seconds=600):
    now = time.time()
    try:
        for fname in os.listdir(ESP_AUDIO_DIR):
            fpath = os.path.join(ESP_AUDIO_DIR, fname)
            if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > max_age_seconds:
                os.remove(fpath)
    except OSError:
        pass


def _write_wav_header(f, data_bytes, sample_rate, bits_per_sample, num_channels):
    """Writes a 44-byte canonical PCM WAV header to file handle `f` at its
    current position (caller is responsible for seeking first)."""
    byte_rate = sample_rate * num_channels * (bits_per_sample // 8)
    block_align = num_channels * (bits_per_sample // 8)
    riff_chunk_size = 36 + data_bytes
    f.write(b"RIFF")
    f.write(riff_chunk_size.to_bytes(4, "little"))
    f.write(b"WAVE")
    f.write(b"fmt ")
    f.write((16).to_bytes(4, "little"))          # fmt chunk size
    f.write((1).to_bytes(2, "little"))            # audio format = PCM
    f.write(num_channels.to_bytes(2, "little"))
    f.write(sample_rate.to_bytes(4, "little"))
    f.write(byte_rate.to_bytes(4, "little"))
    f.write(block_align.to_bytes(2, "little"))
    f.write(bits_per_sample.to_bytes(2, "little"))
    f.write(b"data")
    f.write(data_bytes.to_bytes(4, "little"))


# Hard safety cap so a stuck button (or a malicious client) can't stream
# forever and fill the disk. 60s at 16kHz/16-bit mono is ~1.9MB.
ESP_MAX_UPLOAD_BYTES = 16000 * 2 * 60


@app.route("/esp/transcribe", methods=["POST"])
def esp_transcribe():
    """Body: raw 16-bit PCM mono audio, streamed chunk-by-chunk by the ESP32
    while the button is held (HTTP chunked transfer encoding - the device
    never buffers the whole recording, so there's no fixed max length other
    than the safety cap below). Optional header X-Sample-Rate (default
    16000) tells us the sample rate the device recorded at.

    We stream the incoming bytes straight to disk instead of buffering them
    in memory: write a placeholder WAV header, copy the body in small
    chunks, then seek back and patch the header with the real size once the
    upload is finished.
    """
    if not _check_esp_auth():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    try:
        sample_rate = int(request.headers.get("X-Sample-Rate", "16000"))
    except ValueError:
        sample_rate = 16000
    bits_per_sample = 16
    num_channels = 1

    filename = f"{uuid.uuid4()}.wav"
    filepath = os.path.join(UPLOAD_DIR, filename)

    total_bytes = 0
    try:
        with open(filepath, "wb") as f:
            f.write(b"\x00" * 44)  # placeholder header, patched below
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
            _write_wav_header(f, total_bytes, sample_rate, bits_per_sample, num_channels)
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

    modl.add_message(CHAT_USER, "user", stt["text_original"], stt["lang"], source="esp32")

    return jsonify({
        "ok": True,
        "text_original": stt["text_original"],
        "text_english": stt["text_english"],
        "lang": stt["lang"],
    })


@app.route("/esp/answer", methods=["POST"])
def esp_answer():
    """Body: JSON {text_english, lang}. Runs retrieval + answer generation,
    translates the answer back, synthesises it to a WAV file with espeak-ng,
    and returns a URL the device can GET to stream the audio.

    IMPORTANT: the Groq answer generation step (and storing the answer so
    it shows up on the web page) must NOT depend on espeak-ng being
    installed. Text-to-speech is a separate, optional last step - if it's
    missing or fails, the device still gets back the answer text (and can
    show/skip audio gracefully) instead of the whole request failing
    before Groq is ever even called."""
    if not _check_esp_auth():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    text_english = (data.get("text_english") or "").strip()
    lang = (data.get("lang") or "en").strip()
    if not text_english:
        return jsonify({"ok": False, "error": "Missing text_english."}), 400

    try:
        history = modl.get_messages(CHAT_USER)
        answer_en = modl.answer_query(text_english, history)
        answer_translated = modl.translate_text(answer_en, lang)
    except Exception as e:
        # answer_query()/translate_text() already catch Groq-specific
        # errors internally and return a friendly string instead of
        # raising, so getting here means something unexpected (e.g. the
        # database file itself is unwritable) - log it server-side and
        # tell the device plainly rather than returning an HTML 500 page
        # that the ESP32's JSON parser can't make sense of.
        app.logger.exception("esp_answer: failed generating an answer")
        return jsonify({"ok": False, "error": f"Failed to generate an answer: {e}"}), 500

    # Store + expose the answer immediately - this is what makes it show
    # up on the web chat page, independent of whether speech synthesis
    # below succeeds.
    modl.add_message(CHAT_USER, "assistant", answer_translated, lang, source="esp32")

    audio_url = None
    tts_error = None
    if not modl.espeak_available():
        tts_error = ("espeak-ng is not installed on the server, so the answer "
                     "can't be spoken - see README.md. The text answer was "
                     "still generated and saved.")
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

    return jsonify({
        "ok": True,
        "answer_text": answer_translated,
        "audio_url": audio_url,   # null if TTS wasn't available/failed
        "tts_error": tts_error,   # null on success
    })


@app.route("/esp/audio/<filename>")
def esp_audio(filename):
    # Note for serverless (Vercel) deployments: /tmp is per-container and
    # not shared, so if the GET here lands on a different container than
    # the one that wrote the file in /esp/answer, this 404s. This is a
    # known limitation of running the ESP32 hardware flow on Vercel - see
    # README.md "Deploying to Vercel" for details and the recommended
    # workaround (self-host the server for the physical device, or move
    # audio storage to a real object store / return it inline as base64).
    if not _check_esp_auth():
        abort(401)
    safe_name = os.path.basename(filename)
    filepath = os.path.join(ESP_AUDIO_DIR, safe_name)
    if not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, mimetype="audio/wav", conditional=False)


if __name__ == "__main__":
    # threaded=True so a long-running streamed ESP32 upload on one
    # connection doesn't block the browser chat UI (or a second device).
    #
    # debug=False + use_reloader=False here on purpose: Flask's debug
    # auto-reloader restarts the whole process (and briefly drops all open
    # sockets) whenever it notices a file change - including the audio
    # files this app writes into uploads/ and esp_audio/ while running.
    # That was intermittently making the ESP32's health check fail right
    # after a request, which showed up on the OLED as "Server:
    # UNREACHABLE" even though app.py was still running. Set FLASK_DEBUG=1
    # in your environment if you want the reloader back while editing
    # templates/Python code from a browser tab only (not while a device is
    # attached).
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_mode, use_reloader=debug_mode, host="0.0.0.0", port=5000, threaded=True)
