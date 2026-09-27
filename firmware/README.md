# 26088 ESP32 firmware

Standalone voice-assistant device: hold a button to record a question,
release it, and the device plays back a spoken answer through the speaker.
The OLED shows the current step - "Listening" and "Translating" in English
(the spoken language isn't known yet at that point), then "Searching" and
"Speaking" as a short romanized phrase in the detected language where one is
available (Hindi, Telugu, Tamil, Kannada, Malayalam, Marathi, Bengali,
Gujarati, Punjabi, Urdu), falling back to English otherwise.

Both directions of audio are **streamed**, never buffered whole on the
device: the recording is uploaded live, chunk-by-chunk, while the button is
held (HTTP `Transfer-Encoding: chunked`), and the spoken answer is played
back by streaming the server's response straight into the I2S amplifier.
This means recordings aren't limited by how much RAM the ESP32 has - only by
the `MAX_RECORD_SECONDS` safety cap in the sketch (30s by default, easy to
raise).

## Wiring

| Module | Module pin | ESP32 pin |
|---|---|---|
| **SSD1306 OLED (I2C)** | VCC | 3V3 |
| | GND | GND |
| | SDA | GPIO21 |
| | SCL | GPIO22 |
| **INMP441 mic** | VDD | 3V3 |
| | GND | GND |
| | L/R | GND *(selects left channel)* |
| | WS | GPIO25 |
| | SCK | GPIO26 |
| | SD (data out) | GPIO33 |
| **MAX98357A amp** | VIN | 5V |
| | GND | GND |
| | DIN | GPIO27 |
| | BCLK | GPIO14 |
| | LRC | GPIO4 |
| | SD (shutdown) | 3V3 |
| | GAIN | see below - depends on your speaker |
| | +/- speaker terminals | your speaker leads |
| **Button** (leg 2 to GND) | ASK | GPIO13 |

## Speaker volume / GAIN wiring (important for small speakers)

The GAIN pin sets the MAX98357A's fixed analog gain. Per the datasheet:

| GAIN pin wiring | Gain |
|---|---|
| 100kΩ resistor to VIN | 3dB (quietest) |
| Direct to VIN | 6dB |
| Floating (not connected) | 9dB (chip default) |
| Direct to GND | 12dB |
| 100kΩ resistor to GND | 15dB (loudest) |

**Tying GAIN straight to GND (as an earlier version of this doc said) gives
12dB, not 9dB** - the loudest common setting other than the 15dB option.
That's too much for a small **8Ω / 0.5W speaker**: at 12dB gain the amp can
put out several times that speaker's rated power, which distorts the sound
and can overheat or damage the speaker/voice coil over time.

For an 8Ω/0.5W speaker, wire **GAIN directly to VIN (6dB)** - simplest, no
resistor needed - or, for extra headroom, add a 100kΩ resistor between
GAIN and VIN for **3dB**. The firmware also applies its own `SPK_VOLUME`
software scaling (0.6 by default, see the top of the `.ino` file) on top
of whatever the hardware gain is, as a second layer of protection - lower
it further if the speaker still sounds strained at full volume.

## Diagnosing "no audio output"

If nothing plays through the speaker, narrow down where the problem is
before assuming it's a firmware bug:

1. **Test the speaker wiring directly, with no network involved.** Set
   `PLAY_STARTUP_TEST_TONE` to `true` near the top of the `.ino` file and
   reflash - the device will play a 1kHz test tone for ~1.2 seconds right
   at boot, before it even tries to connect to WiFi.
   - Tone plays: the MAX98357A, GAIN/SD wiring, and speaker are all fine -
     the problem is downstream (server TTS, network, or the answer never
     reaching the "Speaking" step). Set the flag back to `false` once
     confirmed.
   - Tone doesn't play: recheck DIN/BCLK/LRC against GPIO27/14/4, confirm
     SD is tied to 3V3 (not left floating - floating SD can leave the amp
     shut down), and confirm the speaker leads are actually on the +/-
     output pads, not the input side.
2. **Check the OLED status on a real request.** If it gets to "Speaking"
   at all (rather than stopping at "Answered - no voice - see app"), audio
   *was* generated server-side and the device believes it's playing it -
   at that point the issue is almost always wiring/volume, not code. If it
   stops at "no voice - see app" instead, the server couldn't synthesise
   speech at all (see the TTS section in the main README) - that's a
   server-side / configuration issue, not a wiring one.
3. **Check Serial output at 115200 baud** for `audio GET failed` or
   `audio: short header` messages, which point at a network/server
   response problem rather than the speaker itself.

## Diagnosing choppy / breaking-up audio

If audio plays but stutters, clicks, or cuts in and out rather than being
silent or wrong, that's a streaming-timing issue, not a wiring one:

- The device buffers audio in relatively large chunks (4096 bytes, about
  128ms) and keeps roughly 380ms of decoded audio queued in the I2S
  hardware buffer (`setupSpeakerI2S()` in the `.ino`), specifically so a
  normal Wi-Fi hiccup of 100-200ms doesn't starve playback. If you still
  hear breaking audio on your network:
  - Move the device closer to the router, or onto a less congested Wi-Fi
    channel - weak signal causes the retries/stalls this buffering is
    meant to absorb, and a bad enough connection can still exceed it.
  - Check the server isn't heavily loaded or far away on a slow link -
    the device downloads the whole answer as one HTTP response, so a slow
    server connection shows up as the same kind of stutter as a slow
    Wi-Fi link.
  - If you deliberately want more headroom, `dma_buf_count` /
    `dma_buf_len` in `setupSpeakerI2S()` and `CHUNK` in `playAudioUrl()`
    can both be raised further (more RAM buffered, more resilience to
    stalls, at the cost of a longer delay before playback visibly reacts
    to anything going wrong).

## Arduino IDE setup

1. Install the **esp32** board package (Boards Manager) - version 2.x of the
   Arduino-ESP32 core is recommended; the legacy `driver/i2s.h` API used
   here may need adjustment on newer 3.x cores.
2. Select board **ESP32 Dev Module**.
3. Install these libraries via Library Manager:
   - **Adafruit GFX Library**
   - **Adafruit SSD1306**
   - **ArduinoJson** (by Benoit Blanchon, v6 or v7)
4. Open `esp32_voice_assistant.ino` and edit the top of the file:
   - `WIFI_SSID`, `WIFI_PASSWORD`
   - `SERVER_URL` - the Flask server's address, e.g.
     `http://192.168.1.50:5000` (the device and server must be on the same
     network, or the server must otherwise be reachable from the device)
   - `API_KEY` - only needed if you set `ESP_API_KEY` in the server's `.env`
5. Flash to the board, then open the Serial Monitor at 115200 baud to see
   connection status and any errors.

## Server-side requirement: espeak-ng

The device needs the server to convert answer text into a WAV file it can
stream over I2S. This is done with **espeak-ng**, a free offline
text-to-speech engine that ships voices for 100+ languages - Hindi,
Tamil, Telugu, and the rest of the Indian languages, plus most major world
languages (French, Japanese, Mandarin, Arabic, and so on) - no
internet-dependent TTS API required. The server picks whichever espeak-ng
voice matches the language Whisper detected automatically (see the main
`README.md`), so you don't need to configure a language list by hand.

Install it on the machine running the Flask server:

```bash
# Debian / Ubuntu
sudo apt-get install espeak-ng

# macOS (Homebrew)
brew install espeak-ng
```

If it isn't installed, the answer is still generated and shown on the web
chat page, but the device shows "Answered - no voice, see app" instead of
speaking it (see "Troubleshooting" in the main `README.md`).

## Diagnosing "nothing happens when I press the button"

The idle screen now shows live status instead of just "Hold button to
speak", so a WiFi or server problem is visible at a glance instead of
looking like a dead button:

```
26088
WiFi:   connected
Server: online
------------------------
Hold button to speak
```

- **WiFi: OFFLINE** - the device never joined your network. Check
  `WIFI_SSID` / `WIFI_PASSWORD` at the top of the sketch, and that it's a
  2.4GHz network (the ESP32 doesn't do 5GHz).
- **Server: checking...** - shown briefly right after boot, before the
  first health check completes.
- **Server: UNREACHABLE** - WiFi is fine but `GET /health` on `SERVER_URL`
  isn't responding. Usually means the Flask server isn't running, the IP in
  `SERVER_URL` is wrong or stale (e.g. your PC got a new DHCP lease), or the
  device and server aren't on the same network/subnet.

This is rechecked every 5 seconds while idle, and pressing the button while
either line is bad shows a short explicit message ("No WiFi" / "Server
unreachable") instead of silently doing nothing.

Every stage also logs to Serial (115200 baud) - "Button pressed", "heard:
...", request failures with their HTTP status - open the Serial Monitor if
the OLED alone doesn't explain what's happening. During network waits
(waiting for the transcribe reply, streaming playback) the OLED also shows
animated dots so a slow request looks "busy" rather than frozen.

## How the memory budget works

The ESP32 Devkit V1 has no PSRAM, so the firmware avoids ever buffering a
whole audio file in RAM, in either direction:

- **Recording**: mic samples are read off I2S in small blocks (a few
  hundred bytes) and immediately written out to the server as one HTTP
  chunk each, for as long as the button stays held. Only that one small
  block is ever in memory at a time, so `MAX_RECORD_SECONDS` (30 by
  default) is a pure safety timeout, not a memory limit - raise it freely.
- **Playback**: the answer audio is *streamed* from the server in small
  chunks straight into the I2S driver - the full file is never held in RAM,
  so replies of any length can be played back.

## Connectivity indicators

See "Diagnosing 'nothing happens when I press the button'" above for the
full breakdown of the WiFi/Server status lines shown on the idle screen.
`HEALTH_CHECK_INTERVAL_MS` in the sketch controls how often the idle screen
re-checks the server; lower it for faster feedback at the cost of slightly
more idle network traffic.

## Server requirement for streamed uploads

`/esp/transcribe` reads the chunked request body straight through to disk
(rather than buffering it in memory) and patches the WAV header once the
upload finishes. This relies on Werkzeug's request-body dechunking support,
which ships with Flask 3.x (the version pinned in `requirements.txt`) - no
extra configuration needed, just run the server normally.

## Notes and possible extensions

- Because the reply audio format (sample rate, channels) is read from the
  WAV header sent by the server, changing the server's TTS voice or sample
  rate does not require a firmware change.
- OLED status text uses romanized transliterations for a handful of Indian
  languages (see `STATUS_TABLE` in the sketch) rather than native script,
  since the default Adafruit_GFX font has no Devanagari/Telugu/Tamil
  glyphs. To show real native script instead, switch to the `u8g2` library
  with a Unicode font and extend `showStatus()`; add more entries to
  `STATUS_TABLE` to cover more languages in the meantime.
- The three-step server protocol (`/esp/transcribe`, `/esp/answer`,
  `/esp/audio/<file>`) is intentional: it lets the OLED reflect what the
  server is actually doing at each stage, rather than showing a single
  generic "please wait" for the whole round trip.
- `MIN_PRESS_MS` debounces accidental taps before a network connection is
  even opened; a genuinely too-short recording (<0.25s of captured audio)
  is still caught after the fact in `streamRecordingAndTranscribe()`.
