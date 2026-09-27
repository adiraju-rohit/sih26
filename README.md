# 26088

A Flask voice assistant for cooperative-society member support. There is no
login/signup any more — opening the site goes straight to the chatbot. The
same conversation is shared between the browser page and the ESP32
hardware device: whatever either one says (typed, spoken into the browser
mic, or spoken into the device mic) shows up as text on the web page.

## ⚠️ Rotate your Groq API key

The `.env` file you had committed contains a real `GROQ_API_KEY`. Since
that key has now been shared outside your machine, treat it as compromised:
go to https://console.groq.com/keys, revoke it, generate a new one, and put
the new key in `.env` before deploying this anywhere other people can see
your repo/screen.

## What changed in this update

Three things were fixed/added:

1. **"Response not displayed, even as text" (intermittent).** `api_text_message`
   and `api_voice_message` used to call Groq for the answer *before* wrapping
   that call in its own error handling. If a Groq call raised (network
   hiccup, rate limit, slow response) or the database happened to be
   unwritable, the whole request died with a bare 500 and the browser had
   nothing to show — the question you'd already asked (saved to the DB
   first) appeared, but no reply ever did. Both routes now always save
   *some* assistant reply — the real answer, or a plain "something went
   wrong" message if generation failed — so a reply is never silently
   missing anymore.
2. **Text-to-speech wasn't audible, in the browser or on the speaker.**
   - **Browser:** some browsers (notably iOS Safari) only allow
     `speechSynthesis.speak()` when called synchronously inside a user
     gesture — calling it after an `await fetch(...)` can be silently
     ignored there. Every assistant reply now also gets a small **Play**
     button, which calls `speak()` directly from the click itself and
     works everywhere. The page also "warms up" the browser's voice list
     on load, which fixes a common first-answer-is-silent issue in
     Chrome/Edge/Firefox.
   - **ESP32 speaker:** the wiring table had `GAIN → GND`, documented as
     "9dB" — that's actually **12dB** per the MAX98357A datasheet, too hot
     for an 8Ω/0.5W speaker and can genuinely damage it over time or
     distort badly enough to sound like nothing but noise. See
     `firmware/README.md` → "Speaker volume / GAIN wiring" for the
     corrected wiring (GAIN → VIN, or a 100kΩ resistor to VIN for even
     lower gain), plus a software volume knob and a standalone test-tone
     diagnostic so you can confirm the speaker wiring independently of the
     network/TTS pipeline.
3. **Bhashini support**, with automatic fallback to Groq/espeak-ng — see
   below.

## Getting your Bhashini credentials

Bhashini's onboarding portal is now also branded **"Udyat"** — same portal,
same credentials, just a newer name. If you registered there, "Udyat key"
and "ULCA API key" refer to the same thing.

1. Log in at `https://bhashini.gov.in/ulca/profile`.
2. **User ID**: shown directly on the "My Profile" page, separately from
   the API key list below it. Copy this into `BHASHINI_USER_ID`.
3. **API key**: under the "API Key" / "Generate" section of the same page.
   The value you already have (`9f39263f...`) is this key — put it in
   `BHASHINI_ULCA_API_KEY`.
4. You do **not** need anything else. `BHASHINI_INFERENCE_KEY` in
   `.env.example` is optional — the app fetches a fresh inference token
   per call using just the two credentials above (the officially
   documented flow, and it avoids using a token that might have expired).

## Speech recognition, translation, and text-to-speech: priority chains with automatic fallback

Speech **input** is limited to Indian languages plus English: Hindi,
Bengali, Marathi, Telugu, Tamil, Gujarati, Kannada, Malayalam, and Punjabi
(Urdu is intentionally **not** included). This is enforced in one place —
`ALLOWED_LANGS` / `_clamp_lang()` in `modl.py` — and applied everywhere a
language code flows through the system: the language Whisper detects from
speech, the target language for translation and TTS, and the tag used for
the browser's speechSynthesis voice. Any other language Whisper happens to
guess (background noise, a stray word in another language, etc.) is
treated as English rather than passed through, so it can never reach
translation or TTS with an unsupported code.

`translate_text()` and `synthesize_speech()` in `modl.py` each try a list
of backends in priority order, falling through to the next automatically
on any failure (missing config, network error, timeout) - so one backend
being unavailable never means a language stops working entirely:

**Speech recognition (input):** Groq Whisper only. It handles both the
transcription and the automatic language detection (which of the
supported languages was spoken) in one step, then a second Whisper call
translates it to English if it wasn't already. Check `GET
/esp/health`'s `stt_available` field to confirm it's configured.

**Translation:** Bhashini NMT → Groq chat model.

**Text-to-speech (output)** (used by the ESP32 device, and played
automatically as soon as an answer arrives — no extra tap needed):
1. **Bhashini TTS** — has PRIORITY here. Best quality for Indian
   languages, needs `BHASHINI_USER_ID`/`BHASHINI_ULCA_API_KEY` (see
   above). This is the tier you actually want running in production.
2. **espeak-ng** — fully offline, always available once installed
   (`sudo apt-get install espeak-ng` / `brew install espeak-ng`), and
   (verified directly) produces real audio for every supported language,
   so this tier alone guarantees speech output even with zero internet
   access and no Bhashini setup. It sounds noticeably more robotic than
   Bhashini, which is why it's the fallback rather than the default.
3. **Groq TTS** (`playai-tts`) — last resort. **Important limitation:**
   Groq's hosted TTS models currently only support English (and,
   separately, Arabic) — there is no Hindi/Bengali/Marathi/Telugu/Tamil/
   Gujarati/Kannada/Malayalam/Punjabi voice available from Groq at all.
   `synthesize_speech()` checks the target language first and skips this
   tier automatically for anything other than English, rather than
   sending a non-English answer to a model that can't actually speak it.
   In practice this means: for English answers, Bhashini → espeak-ng →
   Groq TTS all three can potentially fire; for every other supported
   language, only Bhashini and espeak-ng ever will.

Check `GET /esp/health` any time to see exactly which backends are
currently usable: `bhashini_configured`, `espeak_available`,
`groq_tts_available`, and the combined `tts_available`.

**Why speech recognition and translation don't fully overlap in
backends:** a core requirement of this project is detecting the spoken
language automatically. Bhashini's ASR endpoint needs the source language
specified up front — it doesn't auto-detect it the way Whisper does —
which is why Groq Whisper alone handles recognition, while Bhashini leads
for translation and speech output, where it doesn't need that upfront
language guess.

### Toggle: web search vs. local document

`answer_query()` can answer questions two different ways, switchable at
runtime with no restart:

- **`web`** (default) — Groq Compound searches the live web for current
  information before answering.
- **`document`** — Groq answers using only the local `data/knowledge.txt`
  file (TF-IDF chunk retrieval), the original cooperative-society-specific
  behaviour.

Switch it with the **"Web search" / "Knowledge doc"** button in the
top-right of the chat page, or directly via the API:

```bash
# Check the current mode
curl http://localhost:5000/api/settings/answer_mode

# Switch modes
curl -X POST http://localhost:5000/api/settings/answer_mode \
  -H "Content-Type: application/json" -d '{"mode": "document"}'
```

This is one shared server-side setting (not per-user/per-session), so
switching it from the web page also changes how the ESP32 device answers,
since both go through the same `answer_query()` on the server.
`ANSWER_MODE` in `.env` just sets the starting value on boot.

**On the browser side**, the spoken answer plays automatically the moment
it arrives (via the Web Speech API), since it's triggered directly inside
the same button-press/form-submit handler that sent the question — no
separate click needed. Each answer bubble also has a **▶ Play** button, since
some browsers (notably iOS Safari) only allow autoplay speech when it's
triggered by a direct, immediate user action, and can silently skip it if
there was an `await` in between; the button guarantees you can always
replay any answer on demand regardless.

## Setup
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# edit .env and set GROQ_API_KEY to your (new) key
python app.py
```

Open **http://localhost:5000** in a browser — you land straight on the
chat page.

To let the ESP32 device (or another computer on your network) reach it,
you need the server's address on your local network and to allow the port
through your firewall. See "Setting up the server so the ESP32 can reach
it" below — this is the fix for the device showing **"Server:
UNREACHABLE"**.

## Setting up the server so the ESP32 can reach it

The ESP32 talks to the Flask server over your local Wi-Fi, not the
internet, so it needs the server's *local network IP address*, and your
firewall needs to allow the connection in. This is the most common reason
the OLED shows **"Server: UNREACHABLE"** even though `app.py` is running.

1. **Put the ESP32 and the computer running `app.py` on the same Wi-Fi
   network.** The ESP32 only supports 2.4GHz Wi-Fi (not 5GHz), so make
   sure the network you give it in the sketch is a 2.4GHz one.

2. **Find the computer's local IP address** (the machine running
   `python app.py`):
   - Windows: open Command Prompt, run `ipconfig`, look for "IPv4 Address"
     under your active Wi-Fi adapter (something like `192.168.1.42`).
   - macOS: System Settings → Wi-Fi → Details, or run
     `ipconfig getifaddr en0` in Terminal.
   - Linux: run `hostname -I` or `ip addr`.

   Do **not** use `127.0.0.1` or `localhost` — that only means "this same
   device", which is meaningless to the ESP32.

3. **Put that IP into the firmware.** In
   `firmware/esp32_voice_assistant.ino`, set:
   ```cpp
   const char* SERVER_URL = "http://192.168.1.42:5000"; // your real IP + port
   ```
   Re-flash the board after changing this.

4. **Allow the port through your computer's firewall.** By default,
   Windows/macOS firewalls block unsolicited inbound connections to
   Python's dev server, which looks *exactly* like "server unreachable" to
   the device even though the browser on the same machine works fine
   (browser traffic is outbound + local, so it isn't blocked the same way).
   - Windows: the first time you run `app.py`, Windows Defender Firewall
     should pop up asking to allow Python on "Private networks" — click
     **Allow access**. If you missed that prompt or clicked "Cancel", go
     to Control Panel → Windows Defender Firewall → "Allow an app through
     firewall", find Python, and tick the Private box (or add a new inbound
     rule for TCP port 5000).
   - macOS: System Settings → Network → Firewall → Options, allow incoming
     connections for Python, or temporarily turn the firewall off to test.
   - Linux (ufw): `sudo ufw allow 5000/tcp`.

5. **Confirm it from another device first.** Before worrying about the
   ESP32, open `http://<that-ip>:5000/health` from your *phone's* browser
   (on the same Wi-Fi). If that doesn't load, it's a network/firewall
   issue, not an ESP32 or code issue — fix that first.

6. **Router/AP client isolation.** Some routers (especially "Guest"
   Wi-Fi networks, and some public/office networks) enable "client/AP
   isolation", which stops devices on the same Wi-Fi from talking to each
   other even though they can both reach the internet. If steps 1–5 all
   check out and it still fails, try a different network (e.g. a phone
   hotspot) to rule this out.

7. **IP changes on reboot.** Most home routers assign IPs via DHCP, which
   can hand your computer a different IP after it reconnects to Wi-Fi. If
   the device was working and later goes back to "UNREACHABLE", re-check
   step 2 and re-flash if the IP changed. (Setting a DHCP reservation for
   your computer in the router's admin page avoids this.)

Once this is set up, the ESP32's idle screen shows a live status:

```
26088
WiFi:   connected
Server: online
------------------------
Hold button to speak
```

## Deploying to Vercel

The project now includes `vercel.json` and `api/index.py` so it can run as
a Vercel serverless function. The "serverless function has crashed" error
you were seeing was almost certainly the app trying to write
`uploads/`, `esp_audio/`, and `app.db` into the project folder itself —
Vercel's filesystem is **read-only except for `/tmp`**, so those writes
failed and crashed the function. That's fixed: `app.py`/`modl.py` now
detect `VERCEL=1` (Vercel sets this automatically) and use `/tmp`
instead.

### Steps

1. Push this project to a GitHub/GitLab/Bitbucket repo (or use the Vercel
   CLI directly on this folder: `npx vercel`).
2. In the Vercel dashboard, "Add New Project" → import the repo.
3. **Do not upload your `.env` file** — `.vercelignore` excludes it on
   purpose. Instead, add the same variables under Project Settings →
   Environment Variables:
   - `GROQ_API_KEY` (required — and use a freshly rotated key, see the
     warning above)
   - `GROQ_CHAT_MODEL` (optional, defaults to `openai/gpt-oss-120b`)
   - `GROQ_STT_MODEL` (optional, defaults to `whisper-large-v3`)
   - `FLASK_SECRET_KEY` (optional but recommended — any random string)
   - `ESP_API_KEY` (optional, only if you want the ESP32 endpoints to
     require a shared secret)
   - Leave `DB_PATH` **unset** so it falls back to `/tmp/app.db`
     automatically; only set it if you're pointing at a real external
     database (see the next section).
4. Deploy. The chatbot should load at your `*.vercel.app` URL with no
   sign-up/login, same as running it locally.

### Important limitations of running this on Vercel

Vercel functions are stateless and short-lived, which this app's design
(SQLite file + local WAV files) doesn't naturally fit:

- **Chat history is not permanently persistent.** `/tmp` is wiped
  whenever Vercel spins up a new container (which happens often — after
  inactivity, deploys, or scaling events). History survives only while
  the same warm container keeps handling requests. For durable history,
  swap the `sqlite3` calls in `modl.py` for a hosted database (e.g.
  Vercel Postgres, Turso, Supabase) — the functions are already isolated
  behind `get_conn()`/`add_message()`/`get_messages()`, so that's a
  contained change.
- **The ESP32 hardware flow needs a real, always-on server, not
  Vercel.** Two things break in serverless:
  - `espeak-ng` (used for `/esp/answer`'s text-to-speech) isn't
    installed on Vercel's Python runtime and can't be `apt-get install`ed
    there, so `/esp/answer` will return the "espeak-ng is not installed"
    error.
  - Even if TTS worked, the WAV file `/esp/answer` writes to `/tmp` and
    the WAV file `/esp/audio/<file>` later reads back may land on two
    *different* containers, since Vercel doesn't guarantee the same
    instance handles both requests — the audio GET would 404.

  **Recommendation:** deploy the web chatbot to Vercel for browser use,
  and keep running `python app.py` on a normal always-on machine (a
  Raspberry Pi, home server, or small VPS) for the ESP32 device, pointing
  `SERVER_URL` in the firmware at that machine instead of the Vercel URL.
  Both can share the same Groq key; they just can't share the same
  serverless deployment.
- **Request/response size and duration limits.** Vercel enforces a
  request body size limit (a few MB) and a max function duration
  (10s on the Hobby plan, more on Pro) — long voice recordings from the
  browser or slow Groq responses can hit these. This doesn't affect
  typical short question/answer turns.



**Browser:**
1. Hold the mic button (or hold the `S` key) — the browser records audio
   with `MediaRecorder` for as long as it's held.
2. The clip is uploaded to `/api/voice_message`.
3. The backend sends it to Groq's `whisper-large-v3` model: once to
   transcribe in the original language (this also returns the detected
   language code — there's no manual language picker), and once to
   translate to English.
4. The English question is answered using retrieval over
   `data/knowledge.txt` (see below), grounded through the Groq chat model.
5. The English answer is translated back into the member's language.
6. The browser speaks the translated answer with the built-in
   `speechSynthesis` API.

Typed questions (the text box under the mic button) skip translation and
go straight to the assistant in English.

**ESP32 device:** same pipeline, but everything happens as three HTTP
round trips against `/esp/transcribe`, `/esp/answer`, and
`/esp/audio/<file>` instead — see `firmware/README.md` for the full
explanation, wiring, and flashing instructions. Speech input is only
recorded while the push-to-talk button is held, and the recording is
streamed to the server live (never buffered as a whole file on the
device), so there's no fixed maximum length other than a safety timeout.
The OLED shows "Listening", "Translating", "Searching", and "Speaking" at
each stage, in English or a short phrase in the detected language.

Every question and answer — from the browser *or* the device — is stored
in one shared conversation. The chat page polls `/api/messages` every few
seconds so device interactions appear on the web page live, without a
reload.

## Troubleshooting: OLED shows "Error" right after "Translating" / no answer appears anywhere

This was caused by a real bug: `/esp/answer` used to check whether
`espeak-ng` was installed **before** ever calling Groq to generate an
answer. If `espeak-ng` wasn't installed (very likely unless you
specifically installed it — see below), the request failed immediately,
so Groq was never even asked for an answer, nothing was saved, and
nothing showed up on the web chat page either — exactly matching "speech
is recognised but no response is generated, and nothing shows on the
server page."

This is now fixed: the server always generates and saves the answer
first (so it shows up on the web page and gets sent back to the device as
text), and only *afterwards* tries to synthesise speech for it. If speech
synthesis isn't available, the device now shows **"Answered — no voice,
see app"** instead of a bare "Error", and the full text answer is still
on the web page. The firmware's HTTP timeouts were also increased — Groq's
answer + translation calls can take longer than the default 5s timeout,
which was a second way a perfectly good answer could look like a failure.

If you still see this after updating both `app.py` and the firmware:

1. **Check the terminal running `python app.py`.** Any real Groq error
   (invalid/expired API key, a decommissioned model name, no internet
   access from the server) is now logged there, and also becomes visible
   as the "answer" text itself on the web chat page and in the Serial
   monitor, instead of silently failing.
2. **Install espeak-ng** if you want the ESP32 to actually speak answers
   out loud (this was never required for the browser chat, which uses the
   browser's own text-to-speech):
   ```bash
   sudo apt-get install espeak-ng      # Debian/Ubuntu
   brew install espeak-ng              # macOS
   ```
   Then confirm it's picked up: `curl http://localhost:5000/esp/health`
   should show `"tts_available": true`.
3. **Re-flash the ESP32** with the updated `.ino` — the old firmware
   can't understand the new response shape and will keep showing "Error".

## The knowledge base

`data/knowledge.txt` holds short, topic-tagged notes on cooperative
governance (each block starts with `TOPIC:`). `modl.py` includes a small,
dependency-free TF-IDF search (`KnowledgeBase` class) that splits this file
into chunks and, for every question, retrieves the most relevant chunks to
place in the prompt sent to Groq.

To use your own, larger reference file: replace `data/knowledge.txt` (or
point `KNOWLEDGE_FILE` in `.env` at a different path) using the same
`TOPIC: ...` block format, or plain paragraphs separated by blank lines —
both are supported.

## Project structure

```
app.py                Flask routes (chat page + ESP32 device endpoints, no auth)
modl.py                Database, knowledge-base retrieval, Groq calls, offline TTS
api/index.py           Vercel serverless entrypoint (exposes app.py's Flask app)
vercel.json            Vercel build/routing config
.vercelignore          Excludes local-only files (.env, app.db, uploads/) from deploys
data/knowledge.txt     Reference text for retrieval-augmented answers
templates/
  chat.html             Single-page chat UI (voice + text), shared by web + device
static/logo.png
firmware/
  esp32_voice_assistant.ino   ESP32 Devkit V1 firmware
  README.md                    Wiring, libraries, flashing instructions
requirements.txt
.env
.env.example
```

## ESP32 hardware device

The same server also powers a standalone hardware device: an ESP32 Devkit
V1 with an SSD1306 OLED, an INMP441 microphone, a MAX98357A amplifier, and
a push-to-talk button. See `firmware/README.md` for wiring, required
Arduino libraries, and flashing steps.

Speech output for the device tries **Bhashini** first (if configured, see
above) then falls back to **espeak-ng**, a free offline text-to-speech
engine (`sudo apt-get install espeak-ng`) — so the device works without
any internet-connected TTS service configured at all, and gets better
voices automatically if you do configure Bhashini. The server reads
espeak-ng's own voice list at startup (`espeak-ng --voices`) and builds a
language-code lookup from it automatically, so the espeak-ng fallback can
speak **whatever language Whisper detected** - around 100+ languages that
espeak-ng ships voices for, not just a short hard-coded list. If a
detected language has no matching voice in either backend, it falls back
to English rather than failing outright. `GET /esp/health` reports
`bhashini_configured`, `tts_available` (true if *either* backend can
currently produce audio), and `espeak_languages` as a quick sanity check.

## Notes

- Chat history is stored in a local SQLite file (`app.db`), created
  automatically on first run, as a single shared conversation (no
  per-user accounts).
- The interface has no home page, login, or language selector by design;
  language is detected automatically from speech.
- No `.css` or `.js` files are used — all styling and behaviour live
  inline inside `templates/chat.html`.

## Alternatives worth knowing about

Things that would make this more efficient, more robust, or simpler to
run, if you want to go further:

**Software**

- **Swap SQLite for a hosted database** (Turso, Supabase Postgres, Neon)
  if deploying to Vercel long-term — `/tmp` there is wiped on cold start,
  so chat history isn't durable as-is. All DB access already goes through
  a handful of functions in `modl.py` (`get_conn`, `add_message`,
  `get_messages`...), so this is a contained swap, not a rewrite.
- **A real vector store** (e.g. sqlite-vec, Chroma, or a hosted one) would
  scale better than the pure-Python TF-IDF search here once
  `data/knowledge.txt` grows past a few hundred KB — the current approach
  is deliberately dependency-free and fine for a knowledge base this size,
  but re-scores every chunk on every query, which gets slower linearly
  with file size.
- **A cloud neural TTS API with a real key** (Azure Speech, Google Cloud
  TTS, Amazon Polly) would give more consistent Indian-language quality
  than the espeak-ng fallback tier, and — since it's just an HTTPS call
  rather than a native binary — works fine on Vercel too, unlike
  espeak-ng which needs to be installed on the machine. Would slot in as
  another tier in `synthesize_speech()` in `modl.py`, same pattern as the
  existing Bhashini → espeak-ng → Groq TTS chain.
- **Streaming answers** (Groq supports `stream=True`) so the browser/device
  starts hearing the first sentence while the rest is still generating,
  instead of waiting for the whole answer — meaningfully cuts perceived
  latency for longer answers.
- **A wake word instead of push-to-talk** (e.g. the `esp-sr` /
  ESP-Skainet wake-word models, which run on-device) so the button isn't
  needed at all — bigger firmware change, but a common next step for this
  kind of device.

**Hardware**

- **Match the amp/speaker pairing on purpose.** The MAX98357A is happiest
  driving a 4Ω speaker rated at least ~3W (its intended target); an 8Ω/0.5W
  speaker works but only safely at low gain (see the GAIN wiring section
  in `firmware/README.md`) — swapping to a 4Ω 3W speaker would let you use
  the amp closer to its designed operating point instead of deliberately
  turning it down.
- **A dedicated volume potentiometer** inline with the speaker (or an
  I2S amp with a hardware volume control) gives physical volume control
  without touching firmware — simpler for a demo/kiosk setting than
  editing `SPK_VOLUME` and reflashing.
- **An ESP32-S3 with PSRAM** removes the RAM pressure that led to
  streaming uploads/downloads being necessary in the first place, and
  opens the door to on-device wake-word detection and higher-quality
  local audio buffering if you want it later.
- **A physically separate mic and speaker enclosure** (rather than both on
  one small board) reduces the chance of the speaker's own output being
  picked up by the mic mid-conversation — not an issue with strict
  push-to-talk (mic and speaker are never active at the same time here),
  but relevant if you ever move to always-listening/wake-word mode.
