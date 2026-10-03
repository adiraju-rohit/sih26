/*
  26088 - ESP32 voice assistant firmware
  Board: ESP32 Devkit V1

  Flow:
    1. IDLE            - waiting, OLED shows an idle message.
    2. Button held down - LISTENING: mic audio is streamed live to the
                           server over a chunked HTTP POST as it's read off
                           the I2S bus. Nothing is buffered in RAM, so a
                           recording can run as long as the button is held
                           (up to the safety cap below) regardless of PSRAM.
    3. Button released  - TRANSLATING: the chunked upload is closed out and
                           the server replies with the recognised text,
                           translated to English (Whisper STT + translate).
    4. SEARCHING        - POST the English text to /esp/answer
                           (knowledge lookup + answer + speech synthesis).
    5. SPEAKING         - GET the returned WAV file and stream it straight
                           into the MAX98357A amplifier over I2S.
    6. back to IDLE.

  Required Arduino libraries (install via Library Manager):
    - Adafruit GFX Library
    - Adafruit SSD1306
    - ArduinoJson (by Benoit Blanchon)
  Everything else (WiFi, HTTPClient, I2S) is part of the standard ESP32
  Arduino core.

  Streaming upload: the recording step talks to the server using a raw
  WiFiClient with a hand-rolled "Transfer-Encoding: chunked" POST, because
  the Arduino HTTPClient library needs to know the body length up front,
  which isn't possible for a live mic stream of unknown final length.
  Playback (step 5) already streams the reply audio from the server
  straight into I2S in small chunks - the full file is never buffered
  either.

  OLED status text: "Listening" and "Translating" are shown before the
  spoken language is known, so they're in English. Once the server tells us
  the detected language (after the transcribe step), "Searching" and
  "Speaking" are shown as a short *romanized* phrase in that language where
  available (the default Adafruit_GFX font has no Devanagari / Telugu /
  Tamil / etc. glyphs, so full native script needs the u8g2 library with a
  Unicode font instead - this uses Latin transliterations as a lightweight
  middle ground). Falls back to English for unlisted languages.
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <ArduinoJson.h>
#include <driver/i2s.h>
#include <math.h>

// ---------------------------------------------------------------------------
// User configuration - edit these before flashing
// ---------------------------------------------------------------------------

const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// Base URL of the Flask server, e.g. "http://192.168.1.50:5000"
const char* SERVER_URL = "http://192.168.1.50:5000";

// Must match ESP_API_KEY in the server's .env file. Leave as "" if the
// server does not set ESP_API_KEY.
const char* API_KEY = "";

// Safety cap on a single recording, in seconds, so a stuck button can't
// stream forever. There's no RAM-based limit anymore since audio is
// streamed live rather than buffered.
#define MAX_RECORD_SECONDS 30

// Ignore accidental taps shorter than this.
#define MIN_PRESS_MS 150

// Software volume scaling applied to every sample before it's written to
// the amplifier, on top of whatever gain the GAIN pin sets in hardware
// (see firmware/README.md - "Speaker volume / GAIN wiring" for the
// 8ohm/0.5W speaker this project was wired for). 1.0 = no extra
// attenuation; lower values reduce loudness and clipping risk. Tune this
// to taste once the GAIN pin is wired correctly - it's a cheap way to
// fine-tune loudness without re-soldering.
#define SPK_VOLUME 0.6f

// Set to true to play a 1kHz test tone directly out of the speaker at
// boot, bypassing WiFi/the server entirely. Use this to confirm the
// MAX98357A wiring and I2S pins are correct before debugging anything
// network-related - see firmware/README.md "Diagnosing no audio output".
#define PLAY_STARTUP_TEST_TONE false

// ---------------------------------------------------------------------------
// Pin map (matches the wiring table)
// ---------------------------------------------------------------------------

// OLED (I2C)
#define OLED_SDA        21
#define OLED_SCL        22
#define OLED_WIDTH      128
#define OLED_HEIGHT     64
#define OLED_ADDR       0x3C

// INMP441 microphone (I2S RX, I2S_NUM_0)
#define MIC_WS          25   // word select
#define MIC_SCK         26   // bit clock
#define MIC_SD          33   // data out (mic) / data in (ESP32)

// MAX98357A amplifier (I2S TX, I2S_NUM_1)
#define SPK_DIN         27   // data in (amp) / data out (ESP32)
#define SPK_BCLK        14
#define SPK_LRC         4

// Push-to-talk button (other leg to GND)
#define BTN_ASK         13

// ---------------------------------------------------------------------------
// Globals
// ---------------------------------------------------------------------------

Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &Wire, -1);

#define MIC_SAMPLE_RATE 16000

enum State { ST_IDLE, ST_LISTENING, ST_TRANSLATING, ST_SEARCHING, ST_SPEAKING, ST_ERROR };
State state = ST_IDLE;

// ---------------------------------------------------------------------------
// Localized (romanized) OLED status words
// ---------------------------------------------------------------------------

struct StatusWords {
  const char* langCode;    // ISO-639-1 code returned by the server
  const char* searching;
  const char* speaking;
};

// Short romanized phrases - renderable with the default Adafruit_GFX font.
// Extend this table to add more languages; unlisted codes fall back to
// English automatically.
const StatusWords STATUS_TABLE[] = {
  { "hi", "Khoj rahe",   "Bol rahe"     },  // Hindi
  { "te", "Vetukutunna", "Matladutunna" },  // Telugu
  { "ta", "Thedukiren",  "Pesukiren"    },  // Tamil
  { "kn", "Huduka",      "Matadutide"   },  // Kannada
  { "ml", "Thedunnu",    "Parayunnu"    },  // Malayalam
  { "mr", "Shodhtoy",    "Bolto"        },  // Marathi
  { "bn", "Khujchi",     "Bolchi"       },  // Bengali
  { "gu", "Shodhi rahya","Bolu chu"     },  // Gujarati
  { "pa", "Labh raha",   "Bol raha"     },  // Punjabi
  { "ur", "Talash",      "Bol raha"     },  // Urdu
};
const size_t STATUS_TABLE_LEN = sizeof(STATUS_TABLE) / sizeof(STATUS_TABLE[0]);

String currentLang = "en";

// ---------------------------------------------------------------------------
// Connectivity status - shown persistently on the idle screen so it's
// obvious whether the device just isn't reaching the server (as opposed to
// silently doing nothing when the button is pressed).
// ---------------------------------------------------------------------------

bool wifiConnected = false;
bool serverReachable = false;
bool serverCheckedOnce = false;
unsigned long lastHealthCheckMs = 0;
#define HEALTH_CHECK_INTERVAL_MS 5000

const char* localizedSearching() {
  for (size_t i = 0; i < STATUS_TABLE_LEN; i++) {
    if (currentLang.equalsIgnoreCase(STATUS_TABLE[i].langCode)) return STATUS_TABLE[i].searching;
  }
  return "Searching";
}

const char* localizedSpeaking() {
  for (size_t i = 0; i < STATUS_TABLE_LEN; i++) {
    if (currentLang.equalsIgnoreCase(STATUS_TABLE[i].langCode)) return STATUS_TABLE[i].speaking;
  }
  return "Speaking";
}

// ---------------------------------------------------------------------------
// OLED helpers
// ---------------------------------------------------------------------------

void showStatus(const char* line1, const char* line2 = nullptr) {
  display.clearDisplay();
  display.setTextSize(2);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(4, 20);
  display.println(line1);
  if (line2) {
    display.setTextSize(1);
    display.setCursor(4, 46);
    display.println(line2);
  }
  display.display();
}

void showIdle() {
  display.clearDisplay();
  display.setTextSize(2);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(20, 0);
  display.println("26088");

  display.setTextSize(1);
  display.setCursor(0, 22);
  display.print("WiFi:   ");
  display.println(wifiConnected ? "connected" : "OFFLINE");

  display.setCursor(0, 34);
  display.print("Server: ");
  if (!wifiConnected) {
    display.println("--");
  } else if (!serverCheckedOnce) {
    display.println("checking...");
  } else {
    display.println(serverReachable ? "online" : "UNREACHABLE");
  }

  display.drawFastHLine(0, 46, OLED_WIDTH, SSD1306_WHITE);

  display.setCursor(4, 52);
  if (wifiConnected && serverReachable) {
    display.println("Hold button to speak");
  } else if (!wifiConnected) {
    display.println("Check WiFi credentials");
  } else {
    display.println("Check server / network");
  }
  display.display();
}

// Cheap animated "..." indicator so a long blocking network call (waiting
// for the server's reply, streaming playback, etc.) still visibly updates
// instead of looking frozen. Call this from inside wait loops; it only
// actually redraws the screen every ~300ms.
void showBusy(const char* line1) {
  static unsigned long lastAnimMs = 0;
  static int dotCount = 0;
  unsigned long now = millis();
  if (now - lastAnimMs < 300) return;
  lastAnimMs = now;
  dotCount = (dotCount + 1) % 4;
  String dots;
  for (int i = 0; i < dotCount; i++) dots += '.';
  showStatus(line1, dots.c_str());
}

// ---------------------------------------------------------------------------
// I2S setup
// ---------------------------------------------------------------------------

void setupMicI2S() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = MIC_SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,   // INMP441 sends 24-bit data in a 32-bit slot
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,    // L/R pin is tied to GND -> left channel
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 4,
    .dma_buf_len = 256,
    .use_apll = false,
    .tx_desc_auto_clear = false,
    .fixed_mclk = 0
  };
  i2s_pin_config_t pins = {
    .bck_io_num = MIC_SCK,
    .ws_io_num = MIC_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = MIC_SD
  };
  i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &pins);
  i2s_zero_dma_buffer(I2S_NUM_0);
}

void setupSpeakerI2S() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = 16000,                            // updated per-file at playback time
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,     // stereo frames; mono source is duplicated L=R
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 6,
    .dma_buf_len = 256,
    .use_apll = false,
    .tx_desc_auto_clear = true,
    .fixed_mclk = 0
  };
  i2s_pin_config_t pins = {
    .bck_io_num = SPK_BCLK,
    .ws_io_num = SPK_LRC,
    .data_out_num = SPK_DIN,
    .data_in_num = I2S_PIN_NO_CHANGE
  };
  i2s_driver_install(I2S_NUM_1, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_1, &pins);
  i2s_zero_dma_buffer(I2S_NUM_1);
}

// ---------------------------------------------------------------------------
// URL parsing (very small helper - just enough for "http://host:port")
// ---------------------------------------------------------------------------

void parseServerUrl(String &host, uint16_t &port) {
  String url = String(SERVER_URL);
  if (url.startsWith("http://")) url = url.substring(7);
  else if (url.startsWith("https://")) url = url.substring(8); // not actually supported by WiFiClient below

  int slashIdx = url.indexOf('/');
  String hostPort = slashIdx >= 0 ? url.substring(0, slashIdx) : url;

  int colonIdx = hostPort.indexOf(':');
  if (colonIdx >= 0) {
    host = hostPort.substring(0, colonIdx);
    port = (uint16_t) hostPort.substring(colonIdx + 1).toInt();
  } else {
    host = hostPort;
    port = 80;
  }
}

// ---------------------------------------------------------------------------
// Streamed recording upload
//
// Opens a raw TCP connection to the server and POSTs the mic audio with
// "Transfer-Encoding: chunked" as it comes off the I2S bus, one small chunk
// at a time, for as long as the button stays held. This means the device
// never needs to allocate a buffer for the whole recording - only a few
// hundred bytes at a time - so recordings of any length (up to the safety
// timeout) are possible even without PSRAM.
// ---------------------------------------------------------------------------

bool streamRecordingAndTranscribe(String &textEnglish, String &lang) {
  String host;
  uint16_t port;
  parseServerUrl(host, port);

  WiFiClient client;
  client.setTimeout(8000);
  if (!client.connect(host.c_str(), port)) {
    Serial.println("streamRecording: connect failed");
    return false;
  }

  // --- Request line + headers ---
  client.print("POST /esp/transcribe HTTP/1.1\r\n");
  client.print("Host: " + host + "\r\n");
  client.print("Content-Type: application/octet-stream\r\n");
  client.print("Transfer-Encoding: chunked\r\n");
  client.print("X-Sample-Rate: " + String(MIC_SAMPLE_RATE) + "\r\n");
  if (strlen(API_KEY) > 0) {
    client.print("X-Api-Key: " + String(API_KEY) + "\r\n");
  }
  client.print("Connection: close\r\n");
  client.print("\r\n");

  // --- Body: one HTTP chunk per I2S read, while the button is held ---
  int32_t i2sReadBuf[128];      // raw 32-bit I2S slots
  int16_t pcmChunk[128];        // converted 16-bit PCM samples
  size_t totalPcmBytes = 0;
  size_t maxPcmBytes = (size_t)MIC_SAMPLE_RATE * 2 * MAX_RECORD_SECONDS;

  while (digitalRead(BTN_ASK) == LOW && totalPcmBytes < maxPcmBytes) {
    size_t bytesRead = 0;
    i2s_read(I2S_NUM_0, (void*)i2sReadBuf, sizeof(i2sReadBuf), &bytesRead, portMAX_DELAY);
    int samples = bytesRead / 4; // 32-bit slots
    if (samples <= 0) continue;

    for (int i = 0; i < samples; i++) {
      pcmChunk[i] = (int16_t)(i2sReadBuf[i] >> 14); // 32-bit slot -> 16-bit PCM
    }
    size_t pcmBytes = samples * 2;
    totalPcmBytes += pcmBytes;

    // Write one HTTP chunk: <hex length>\r\n<data>\r\n
    client.printf("%X\r\n", (unsigned int)pcmBytes);
    client.write((const uint8_t*)pcmChunk, pcmBytes);
    client.print("\r\n");
  }

  // Terminating zero-length chunk closes the body.
  client.print("0\r\n\r\n");

  // Button has been released (or the safety cap was hit) - the upload is
  // done and we're now waiting on the server's speech-recognition reply.
  state = ST_TRANSLATING;
  showStatus("Translating");

  if (totalPcmBytes < (MIC_SAMPLE_RATE / 4) * 2) { // < ~0.25s captured
    client.stop();
    return false;
  }

  // --- Read the response: status line, headers, then body ---
  unsigned long t0 = millis();
  while (!client.available() && client.connected() && millis() - t0 < 25000) {
    showBusy("Translating"); // throttles itself, safe to call every pass
    delay(10);
  }

  String statusLine = client.readStringUntil('\n');
  int statusCode = 0;
  int firstSpace = statusLine.indexOf(' ');
  if (firstSpace >= 0) statusCode = statusLine.substring(firstSpace + 1, firstSpace + 4).toInt();

  // Skip headers.
  while (client.connected() || client.available()) {
    String line = client.readStringUntil('\n');
    if (line == "\r" || line.length() == 0) break;
  }

  // Read the rest as the JSON body (server sets Connection: close, so read
  // until the socket closes).
  String body;
  t0 = millis();
  while ((client.connected() || client.available()) && millis() - t0 < 8000) {
    while (client.available()) {
      body += (char) client.read();
      t0 = millis();
    }
  }
  client.stop();

  if (statusCode != 200) {
    Serial.printf("transcribe failed, code=%d body=%s\n", statusCode, body.c_str());
    return false;
  }

  StaticJsonDocument<2048> doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err || !doc["ok"].as<bool>()) {
    Serial.println("transcribe: bad response");
    return false;
  }
  textEnglish = doc["text_english"].as<String>();
  lang        = doc["lang"].as<String>();
  return true;
}

// ---------------------------------------------------------------------------
// Networking - answer + playback (small, known-size requests, so the
// regular HTTPClient library is fine here)
// ---------------------------------------------------------------------------

void addAuthHeader(HTTPClient& http) {
  if (strlen(API_KEY) > 0) {
    http.addHeader("X-Api-Key", API_KEY);
  }
}

// Quick reachability check against GET /esp/health (matches the server's
// ESP-device auth check, so it needs the API key header too if one is
// configured). Short timeouts so this never stalls the UI for long.
bool checkServerHealth() {
  HTTPClient http;
  String url = String(SERVER_URL) + "/esp/health";
  http.setConnectTimeout(1500);
  http.setTimeout(1500);
  http.begin(url);
  addAuthHeader(http);
  int code = http.GET();
  http.end();
  return code == 200;
}

// Called every loop() pass while idle. Cheaply throttles itself so the
// actual WiFi/server checks only happen every HEALTH_CHECK_INTERVAL_MS.
// Refreshes the idle screen whenever status changes so it's always
// obvious whether the device can currently reach the server.
void updateConnectivityStatus(bool forceRedraw = false) {
  bool wasWifi = wifiConnected;
  bool wasServer = serverReachable;

  wifiConnected = (WiFi.status() == WL_CONNECTED);

  unsigned long now = millis();
  bool dueForCheck = (now - lastHealthCheckMs >= HEALTH_CHECK_INTERVAL_MS) || !serverCheckedOnce;

  if (wifiConnected && dueForCheck) {
    lastHealthCheckMs = now;
    bool reachable = checkServerHealth();
    if (reachable != serverReachable || !serverCheckedOnce) {
      Serial.printf("Server health check: %s\n", reachable ? "OK" : "unreachable");
    }
    serverReachable = reachable;
    serverCheckedOnce = true;
  } else if (!wifiConnected) {
    serverReachable = false;
  }

  if (forceRedraw || wasWifi != wifiConnected || wasServer != serverReachable) {
    if (state == ST_IDLE) showIdle();
  }
}

// Step 2: send the English text, get back the answer + an audio URL.
// hasAudio is set to false (not a failure) when the server generated an
// answer but couldn't synthesise speech for it (e.g. espeak-ng missing on
// the server) - the answer still exists and shows on the web chat page,
// there's just nothing to play back on the device this time.
bool callAnswer(const String &textEnglish, const String &lang, String &audioUrl, bool &hasAudio) {
  hasAudio = false;
  HTTPClient http;
  String url = String(SERVER_URL) + "/esp/answer";
  // Groq does two sequential model calls here (answer + translation) on
  // top of normal network latency, which can comfortably take longer than
  // HTTPClient's ~5s default timeout - that made a perfectly valid,
  // in-progress answer look like a failure ("Error" on the OLED) purely
  // because the ESP32 gave up waiting too early. 20s gives real requests
  // room to finish.
  http.setConnectTimeout(5000);
  http.setTimeout(20000);
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  addAuthHeader(http);

  StaticJsonDocument<1024> reqDoc;
  reqDoc["text_english"] = textEnglish;
  reqDoc["lang"] = lang;
  String payload;
  serializeJson(reqDoc, payload);

  int code = http.POST(payload);
  String body = http.getString();
  http.end();

  if (code != 200) {
    Serial.printf("answer failed, code=%d body=%s\n", code, body.c_str());
    return false;
  }

  // Answers can run to a few hundred words once translated into scripts
  // like Hindi/Tamil (multi-byte UTF-8), so give the parser plenty of
  // room - a too-small buffer silently truncates/corrupts the JSON,
  // which also looked like a generic failure before.
  DynamicJsonDocument doc(8192);
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    Serial.printf("answer: JSON parse failed: %s\n", err.c_str());
    return false;
  }
  if (!doc["ok"].as<bool>()) {
    const char* srvError = doc["error"] | "(no error field)";
    Serial.printf("answer: server reported failure: %s\n", srvError);
    return false;
  }

  if (!doc["audio_url"].isNull()) {
    audioUrl = doc["audio_url"].as<String>();
    hasAudio = audioUrl.length() > 0;
  }
  if (!doc["tts_error"].isNull()) {
    Serial.printf("answer: no audio this time: %s\n", doc["tts_error"].as<const char*>());
  }
  return true; // the answer itself was generated successfully either way
}

// Step 3: stream the WAV file straight into the I2S amplifier.
bool playAudioUrl(const String &audioUrlPath) {
  HTTPClient http;
  String url = String(SERVER_URL) + audioUrlPath;
  http.begin(url);
  addAuthHeader(http);

  int code = http.GET();
  if (code != 200) {
    Serial.printf("audio GET failed, code=%d\n", code);
    http.end();
    return false;
  }

  WiFiClient* stream = http.getStreamPtr();

  // Read the 44-byte WAV header to learn the real sample rate / channels.
  const size_t WAV_HEADER_SIZE = 44;
  uint8_t header[WAV_HEADER_SIZE];
  size_t got = 0;
  unsigned long t0 = millis();
  while (got < WAV_HEADER_SIZE && millis() - t0 < 5000) {
    if (stream->available()) {
      int n = stream->read(header + got, WAV_HEADER_SIZE - got);
      if (n > 0) got += n;
    }
  }
  if (got < WAV_HEADER_SIZE) {
    Serial.println("audio: short header, aborting");
    http.end();
    return false;
  }

  uint16_t numChannels;
  uint32_t sampleRate;
  uint16_t bitsPerSample;
  memcpy(&numChannels, header + 22, 2);
  memcpy(&sampleRate, header + 24, 4);
  memcpy(&bitsPerSample, header + 34, 2);
  if (numChannels == 0) numChannels = 1;
  if (bitsPerSample == 0) bitsPerSample = 16;

  i2s_set_sample_rates(I2S_NUM_1, sampleRate);

  const size_t CHUNK = 512;
  uint8_t inBuf[CHUNK];
  int16_t stereoOut[CHUNK]; // big enough for CHUNK/2 mono samples -> CHUNK stereo int16 values

  int contentLen = http.getSize();
  int totalRead = 0;

  while (http.connected() && (contentLen < 0 || totalRead < contentLen)) {
    showBusy(localizedSpeaking()); // throttles itself to ~3fps, cheap to call
    size_t avail = stream->available();
    if (!avail) {
      if (!http.connected()) break;
      delay(1);
      continue;
    }
    size_t toRead = avail > CHUNK ? CHUNK : avail;
    int n = stream->readBytes(inBuf, toRead);
    if (n <= 0) break;
    totalRead += n;

    if (bitsPerSample == 16) {
      int sampleCount = n / 2;
      int16_t* samples = (int16_t*)inBuf;
      int outIdx = 0;
      for (int i = 0; i < sampleCount; i++) {
        int16_t scaled = (int16_t)(samples[i] * SPK_VOLUME);
        if (numChannels == 1) {
          stereoOut[outIdx++] = scaled; // left
          stereoOut[outIdx++] = scaled; // right (duplicated)
        } else {
          stereoOut[outIdx++] = scaled;
        }
      }
      size_t bytesWritten = 0;
      i2s_write(I2S_NUM_1, stereoOut, outIdx * 2, &bytesWritten, portMAX_DELAY);
    }
  }

  http.end();
  return true;
}

// ---------------------------------------------------------------------------
// Speaker hardware diagnostic - plays a 1kHz test tone directly, with no
// WiFi or server involved at all. If you hear this but never hear spoken
// answers, the problem is in the network/TTS pipeline, not the wiring; if
// you DON'T hear this, the problem is the MAX98357A wiring, the GAIN/SD
// pins, or the speaker itself. See firmware/README.md.
// ---------------------------------------------------------------------------

void playTestTone(int frequencyHz = 1000, int durationMs = 1200) {
  const int sampleRate = 16000;
  const int samplesTotal = (sampleRate * durationMs) / 1000;
  int16_t stereoBuf[256];

  showStatus("Test tone", "playing...");
  i2s_set_sample_rates(I2S_NUM_1, sampleRate);

  int samplesWritten = 0;
  double phase = 0.0;
  double phaseInc = 2.0 * PI * frequencyHz / sampleRate;

  while (samplesWritten < samplesTotal) {
    int batch = min(128, samplesTotal - samplesWritten);
    for (int i = 0; i < batch; i++) {
      int16_t s = (int16_t)(sin(phase) * 12000 * SPK_VOLUME); // moderate amplitude
      stereoBuf[i * 2] = s;
      stereoBuf[i * 2 + 1] = s;
      phase += phaseInc;
    }
    size_t bytesWritten = 0;
    i2s_write(I2S_NUM_1, stereoBuf, batch * 2 * sizeof(int16_t), &bytesWritten, portMAX_DELAY);
    samplesWritten += batch;
  }
  showIdle();
}

// ---------------------------------------------------------------------------
// Setup / loop
// ---------------------------------------------------------------------------

void connectWifi() {
  showStatus("Connecting", "WiFi...");
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(400);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi connected: " + WiFi.localIP().toString());
  } else {
    Serial.println("\nWiFi connection failed.");
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);

  pinMode(BTN_ASK, INPUT_PULLUP);

  Wire.begin(OLED_SDA, OLED_SCL);
  if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
    Serial.println("SSD1306 not found.");
  }
  display.setRotation(0);
  showStatus("Starting", "26088");

  setupMicI2S();
  setupSpeakerI2S();

  if (PLAY_STARTUP_TEST_TONE) {
    playTestTone();
  }

  connectWifi();
  if (WiFi.status() != WL_CONNECTED) {
    showStatus("WiFi failed", "check credentials");
    delay(3000);
  }

  state = ST_IDLE;
  updateConnectivityStatus(true); // draws the idle screen with real status
}

void loop() {
  if (state == ST_IDLE) {
    updateConnectivityStatus(); // keeps the WiFi/Server lines on the idle screen fresh

    if (digitalRead(BTN_ASK) == LOW) {
      Serial.println("Button pressed.");

      if (!wifiConnected) {
        Serial.println("  -> aborted: WiFi not connected.");
        showStatus("No WiFi", "connection");
        delay(1200);
        showIdle();
        return;
      }
      if (!serverReachable) {
        Serial.println("  -> aborted: server unreachable.");
        showStatus("Server", "unreachable");
        delay(1200);
        showIdle();
        return;
      }

      // Debounce accidental taps before committing to a connection.
      delay(MIN_PRESS_MS);
      if (digitalRead(BTN_ASK) != LOW) {
        Serial.println("  -> released too soon, ignored.");
        return;
      }

      // --- LISTENING (streamed straight to the server) ---
      state = ST_LISTENING;
      Serial.println("  -> recording + streaming to /esp/transcribe ...");
      showStatus("Listening", "release to send");
      String textEnglish, lang;
      // Sets state/OLED to TRANSLATING internally once the button is
      // released and the upload finishes.
      bool gotSpeech = streamRecordingAndTranscribe(textEnglish, lang);

      if (!gotSpeech) {
        Serial.println("  -> no speech / transcribe failed.");
        showStatus("No speech", "detected");
        delay(1200);
        showIdle();
        state = ST_IDLE;
        return;
      }
      currentLang = lang;
      Serial.printf("  -> heard (%s): %s\n", lang.c_str(), textEnglish.c_str());

      // --- SEARCHING ---
      state = ST_SEARCHING;
      Serial.println("  -> querying /esp/answer ...");
      showStatus(localizedSearching(), "Searching");
      String audioUrl;
      bool hasAudio = false;
      if (!callAnswer(textEnglish, lang, audioUrl, hasAudio)) {
        Serial.println("  -> /esp/answer failed.");
        showStatus("Error", "try again");
        delay(1500);
        showIdle();
        state = ST_IDLE;
        return;
      }

      if (!hasAudio) {
        // The answer WAS generated (and is visible on the web chat page) -
        // the server just couldn't turn it into speech this time (e.g.
        // espeak-ng isn't installed there). Don't call this an "Error";
        // say plainly what happened instead of leaving the user thinking
        // the whole thing failed.
        Serial.println("  -> answer generated, but no audio to play (see server logs).");
        showStatus("Answered", "no voice - see app");
        delay(1800);
        showIdle();
        state = ST_IDLE;
        updateConnectivityStatus(true);
        return;
      }

      // --- SPEAKING ---
      state = ST_SPEAKING;
      Serial.println("  -> streaming answer audio for playback ...");
      showStatus(localizedSpeaking(), "Speaking");
      playAudioUrl(audioUrl);
      Serial.println("  -> done, back to idle.");

      state = ST_IDLE;
      updateConnectivityStatus(true);
    }
  }

  delay(10);
}
