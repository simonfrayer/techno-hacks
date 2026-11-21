// Medicine intake + storage monitor: the app logic.
//
// Included from project.yaml `code: globals:`, so it is compiled into the
// generated build/src/main.cpp after the module instances are defined:
//   st7735_tft, microphone, rotary_encoder_knob, button_module,
//   neopixel_grid, aht20_sensor
//
// Flow: hold the button and speak (name, expiry, tablet count) -> audio is
// streamed over USB to the Python bridge -> it answers with the `voice` action
// -> pick a storage place with the knob, press the knob to confirm -> `record`
// event -> the bridge answers `saved` / `error`. Temperature and humidity are
// reported hourly; the status light shows the storage conditions.
//
// Wire protocol (atech events/actions) is documented in ../../CLAUDE.md.
#pragma once
#include <Arduino.h>

namespace med {

// ---------------- state ----------------
enum State { IDLE, RECORDING, UPLOADING, WAIT_VOICE, MENU, WAIT_SAVE, RESULT };
static State state = IDLE;
static const char* const STATE_NAMES[] = {
    "idle", "recording", "uploading", "wait_voice", "menu", "wait_save", "result"};

// Timeouts (ms)
static const unsigned long VOICE_TIMEOUT_MS = 60000;   // transcription can be slow on a laptop
static const unsigned long SAVE_TIMEOUT_MS = 15000;    // spec: TRY AGAIN after 15 s
static const unsigned long UPLOAD_TIMEOUT_MS = 20000;  // no host reading the port
static const unsigned long RESULT_SHOW_MS = 4000;
static const unsigned long RELEASE_DEBOUNCE_MS = 60;

// ---------------- audio ----------------
// The microphone delivers 16 kHz int16. We keep 8 kHz (2:1, pair-averaged) as
// 8-bit G.711 mu-law: same memory as linear 8-bit, but no clipping and quiet
// speech keeps its detail.
static const int AUDIO_RATE = 8000;
static const int MAX_TALK_SECONDS = 15;                 // limit of one hold
static const int AUDIO_MAX = AUDIO_RATE * MAX_TALK_SECONDS;
static uint8_t audioBuf[AUDIO_MAX];
static volatile int audioLen = 0;                       // written by the capture task
static volatile bool capturing = false;
static volatile bool captureFull = false;
static int capCarry = 0;
static bool capHasCarry = false;

static const int CHUNK = 96;                            // bytes per line -> 128 base64 chars
static int audioSent = 0, audioSeq = 0;
static uint32_t recordId = 0;

static inline uint8_t linearToUlaw(int sample) {
  const int BIAS = 0x84, CLIP = 32635;
  int sign = 0;
  if (sample < 0) { sign = 0x80; sample = -sample; }
  if (sample > CLIP) sample = CLIP;
  sample += BIAS;
  int exponent = 7;
  for (int mask = 0x4000; (sample & mask) == 0 && exponent > 0; mask >>= 1) exponent--;
  int mantissa = (sample >> (exponent + 3)) & 0x0F;
  return (uint8_t)~(sign | (exponent << 4) | mantissa);
}

// Runs on the microphone's capture task (core 0). No Serial, no module calls.
static void IRAM_ATTR onMicAudio(const int16_t* smp, int count, void*) {
  if (!capturing) { capHasCarry = false; return; }
  int len = audioLen;
  for (int i = 0; i < count; i++) {
    if (!capHasCarry) { capCarry = smp[i]; capHasCarry = true; continue; }
    capHasCarry = false;
    if (len >= AUDIO_MAX) { captureFull = true; capturing = false; break; }
    audioBuf[len++] = linearToUlaw((capCarry + (int)smp[i]) / 2);
  }
  audioLen = len;
}

// ---------------- record fields / config ----------------
static char fName[48] = "", fExpiry[24] = "", fQty[12] = "";

static const int MAX_PLACES = 10;
static char places[MAX_PLACES][24] = {"Pharmacy shelf A", "Pharmacy shelf B", "Fridge",
                                      "Cabinet 1", "Cabinet 2"};
static int numPlaces = 5;
static int sel = 0;
static bool knobReverse = false;

static float tMin = 2.0f, tMax = 25.0f, hMax = 60.0f;
static float lastT = NAN, lastH = NAN;
static int climateLevel = -1;          // 0 ok, 1 near limit, 2 over, -1 unknown
static uint32_t reportMinutes = 60;
static unsigned long lastReportMs = 0;
static bool firstReportDone = false;

static int resultKind = 0;             // 1 saved, 2 error, 3 timeout
static int lastSaveKind = 0;           // top row: 0 none, 1 saved (green), 2 not saved (red)
static char resultMsg[40] = "";
static unsigned long stateSince = 0;
static bool screenDirty = true, ledDirty = true;
static unsigned long lastUploadDraw = 0;
static int lastShownSec = -1;

static EveryTimer frameTimer, climateTimer;

// ---------------- helpers ----------------
// Write one whole line, only if the USB send buffer has room for all of it.
// (The firmware runs Serial with a 0 ms TX timeout, so a line that does not
// fit would otherwise be cut off.) Waits up to waitMs for room.
static bool writeLine(const char* buf, int n, unsigned long waitMs) {
  unsigned long t0 = millis();
  while (Serial.availableForWrite() < n) {
    if (millis() - t0 >= waitMs) return false;
    delay(1);
  }
  Serial.write((const uint8_t*)buf, n);
  return true;
}

// {"type":"event","payload":{"event_type":"state","key":KEY,"value":VALUE_JSON}}
static bool emit(const char* key, const char* valueJson, unsigned long waitMs = 100) {
  char line[256];
  int n = snprintf(line, sizeof(line),
                   "{\"type\":\"event\",\"payload\":{\"event_type\":\"state\",\"key\":\"%s\",\"value\":%s}}\n",
                   key, valueJson);
  if (n <= 0 || n >= (int)sizeof(line)) return false;
  return writeLine(line, n, waitMs);
}

static void setState(State s) {
  state = s;
  stateSince = millis();
  screenDirty = true;
  ledDirty = true;
  char v[24];
  snprintf(v, sizeof(v), "\"%s\"", STATE_NAMES[s]);
  emit("ui_state", v, 20);
}

static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
static void b64(const uint8_t* in, int n, char* out) {
  int o = 0;
  for (int i = 0; i < n; i += 3) {
    uint32_t v = in[i] << 16;
    if (i + 1 < n) v |= in[i + 1] << 8;
    if (i + 2 < n) v |= in[i + 2];
    out[o++] = B64[(v >> 18) & 63];
    out[o++] = B64[(v >> 12) & 63];
    out[o++] = (i + 1 < n) ? B64[(v >> 6) & 63] : '=';
    out[o++] = (i + 2 < n) ? B64[v & 63] : '=';
  }
  out[o] = 0;
}

// copy a JSON-safe version (drop quotes/backslashes/control chars)
static void safeCopy(char* dst, size_t n, const char* src, size_t len) {
  size_t o = 0;
  for (size_t i = 0; i < len && o + 1 < n; i++) {
    char c = src[i];
    if (c == '"' || c == '\\' || (unsigned char)c < 32) continue;
    dst[o++] = c;
  }
  dst[o] = 0;
}

// extract "key": value (string or number) from a flat JSON object
static bool jsonField(const char* js, const char* key, char* out, size_t n) {
  char pat[32];
  snprintf(pat, sizeof(pat), "\"%s\"", key);
  const char* p = strstr(js, pat);
  if (!p) return false;
  p = strchr(p + strlen(pat), ':');
  if (!p) return false;
  p++;
  while (*p == ' ') p++;
  const char* e;
  if (*p == '"') { p++; e = strchr(p, '"'); if (!e) return false; }
  else { e = p; while (*e && *e != ',' && *e != '}') e++; }
  safeCopy(out, n, p, e - p);
  return true;
}

// ---------------- climate ----------------
static void sendClimate(const char* reason) {
  char v[200];
  if (isnan(lastT) || isnan(lastH)) {
    snprintf(v, sizeof(v), "{\"ok\":false,\"reason\":\"%s\"}", reason);
  } else {
    const char* st = climateLevel == 2 ? "alarm" : climateLevel == 1 ? "warning" : "ok";
    snprintf(v, sizeof(v),
             "{\"temp_c\":%.1f,\"humidity\":%.1f,\"status\":\"%s\",\"reason\":\"%s\",\"t_min\":%.1f,\"t_max\":%.1f,\"h_max\":%.1f}",
             lastT, lastH, st, reason, tMin, tMax, hMax);
  }
  emit("climate", v);
}

// Read the cached sensor values and re-grade them against the limits.
// Reports a status change unless `quiet`.
static void readClimate(bool quiet = false) {
  float t = aht20_sensor.readTemperature();
  float h = aht20_sensor.readHumidity();
  if (isnan(t) || isnan(h) || (t == 0.0f && h == 0.0f)) { lastT = lastH = NAN; }
  else { lastT = t; lastH = h; }
  int lvl;
  if (isnan(lastT)) lvl = -1;
  else if (lastT > tMax || lastT < tMin || lastH > hMax) lvl = 2;
  else if (lastT > tMax - 1.0f || lastT < tMin + 1.0f || lastH > hMax - 5.0f) lvl = 1;
  else lvl = 0;
  if (lvl != climateLevel) {
    int prev = climateLevel;
    climateLevel = lvl;
    ledDirty = true;
    screenDirty = true;
    if (!quiet && (prev != -1 || lvl > 0)) sendClimate("status_change");
  }
}

// ---------------- LEDs: top row = record status, rows 1-2 = storage ----------------
static void drawLeds() {
  neopixel_grid.setBrightness(40);
  neopixel_grid.clear();
  uint8_t r = 0, g = 0, b = 0;
  switch (state) {
    case RECORDING: b = 255; break;
    case UPLOADING: case WAIT_VOICE: case WAIT_SAVE: g = 120; b = 255; break;
    default:  // idle/menu/result: hold the last save outcome until the next recording
      if (lastSaveKind == 1) g = 255; else if (lastSaveKind == 2) r = 255;
      break;
  }
  neopixel_grid.setRow(0, r, g, b);
  uint8_t cr = 0, cg = 0, cb = 0;
  if (climateLevel == 0) { cg = 60; }
  else if (climateLevel == 1) { cr = 255; cg = 100; }
  else if (climateLevel == 2) { cr = 255; }
  else { cr = 40; cg = 40; cb = 40; }
  neopixel_grid.setRow(1, cr, cg, cb);
  neopixel_grid.setRow(2, cr, cg, cb);
  neopixel_grid.show();
}

// ---------------- screen (portrait 80x160, top toward the board's top edge) ----------------
static void text(const char* s, int y, uint16_t c, uint8_t size = 1) {
  char buf[16];
  int maxc = size == 1 ? 13 : 6;
  strncpy(buf, s, maxc);
  buf[maxc] = 0;
  st7735_tft.displayText(buf, 1, y, size, c);
}

static void drawScreen() {
  st7735_tft.fillScreen(ST7735_TFT::COLOR_BLACK);
  char b[40];
  // storage strip at the bottom
  if (isnan(lastT)) snprintf(b, sizeof(b), "--.-C --%%");
  else snprintf(b, sizeof(b), "%.1fC %.0f%%", lastT, lastH);
  uint16_t cc = climateLevel == 2 ? ST7735_TFT::COLOR_RED
              : climateLevel == 1 ? ST7735_TFT::COLOR_ORANGE
                                  : ST7735_TFT::COLOR_GREEN;
  if (climateLevel < 0) cc = ST7735_TFT::COLOR_WHITE;
  text(b, 148, cc);

  switch (state) {
    case IDLE:
      text("READY", 4, ST7735_TFT::COLOR_CYAN, 2);
      text("Hold button", 30, ST7735_TFT::COLOR_WHITE);
      text("and say:", 42, ST7735_TFT::COLOR_WHITE);
      text("name,", 58, ST7735_TFT::COLOR_YELLOW);
      text("expiry date,", 70, ST7735_TFT::COLOR_YELLOW);
      text("tablet count", 82, ST7735_TFT::COLOR_YELLOW);
      break;
    case RECORDING:
      text("REC", 4, ST7735_TFT::COLOR_RED, 2);
      text("Speak now...", 30, ST7735_TFT::COLOR_WHITE);
      text("Release when", 50, ST7735_TFT::COLOR_WHITE);
      text("done", 62, ST7735_TFT::COLOR_WHITE);
      snprintf(b, sizeof(b), "%ds / %ds", audioLen / AUDIO_RATE, MAX_TALK_SECONDS);
      text(b, 82, ST7735_TFT::COLOR_YELLOW);
      break;
    case UPLOADING:
      text("SEND", 4, ST7735_TFT::COLOR_CYAN, 2);
      snprintf(b, sizeof(b), "Audio %d%%", audioLen ? (int)(100L * audioSent / audioLen) : 0);
      text(b, 30, ST7735_TFT::COLOR_WHITE);
      break;
    case WAIT_VOICE:
      text("WAIT", 4, ST7735_TFT::COLOR_CYAN, 2);
      text("Understanding", 30, ST7735_TFT::COLOR_WHITE);
      text("your voice...", 42, ST7735_TFT::COLOR_WHITE);
      break;
    case MENU: {
      text(fName, 2, ST7735_TFT::COLOR_YELLOW);
      snprintf(b, sizeof(b), "Exp %s", fExpiry); text(b, 12, ST7735_TFT::COLOR_WHITE);
      snprintf(b, sizeof(b), "Qty %s", fQty);    text(b, 22, ST7735_TFT::COLOR_WHITE);
      text("Place:", 36, ST7735_TFT::COLOR_CYAN);
      int first = sel - 3;
      if (first < 0) first = 0;
      if (first > numPlaces - 8) first = numPlaces - 8 < 0 ? 0 : numPlaces - 8;
      for (int i = first, row = 0; i < numPlaces && row < 8; i++, row++) {
        int y = 48 + row * 12;
        if (i == sel) {
          st7735_tft.fillRect(0, y - 2, 80, 11, ST7735_TFT::COLOR_BLUE);
          text(places[i], y, ST7735_TFT::COLOR_WHITE);
        } else {
          text(places[i], y, 0x8410);
        }
      }
      break;
    }
    case WAIT_SAVE:
      text("SAVE", 4, ST7735_TFT::COLOR_CYAN, 2);
      text("Saving to", 30, ST7735_TFT::COLOR_WHITE);
      text(places[sel], 42, ST7735_TFT::COLOR_YELLOW);
      break;
    case RESULT:
      if (resultKind == 1) {
        text("SAVED", 4, ST7735_TFT::COLOR_GREEN, 2);
      } else {
        text("TRY", 4, ST7735_TFT::COLOR_RED, 2);
        text("AGAIN", 22, ST7735_TFT::COLOR_RED, 2);
      }
      text(resultMsg, 44, ST7735_TFT::COLOR_WHITE);
      text(fName, 58, ST7735_TFT::COLOR_YELLOW);
      break;
  }
  st7735_tft.display();
}

static void showResult(int kind, const char* msg) {
  resultKind = kind;
  safeCopy(resultMsg, sizeof(resultMsg), msg, strlen(msg));
  setState(RESULT);
}

// ---------------- record / audio ----------------
static void sendRecord() {
  char v[200];
  snprintf(v, sizeof(v), "{\"id\":%lu,\"name\":\"%s\",\"expiry\":\"%s\",\"quantity\":\"%s\",\"place\":\"%s\"}",
           (unsigned long)recordId, fName, fExpiry, fQty, places[sel]);
  setState(WAIT_SAVE);
  emit("record", v, 200);
}

// One audio chunk as an event line. Returns false if the USB buffer had no room.
static bool sendAudioChunk(int seq, unsigned long waitMs) {
  int off = seq * CHUNK;
  if (off >= audioLen) return true;
  int n = audioLen - off;
  if (n > CHUNK) n = CHUNK;
  char enc[CHUNK / 3 * 4 + 8];
  b64(audioBuf + off, n, enc);
  char v[200];
  snprintf(v, sizeof(v), "{\"id\":%lu,\"seq\":%d,\"d\":\"%s\"}", (unsigned long)recordId, seq, enc);
  return emit("audio", v, waitMs);
}

static int audioChunkCount() { return (audioLen + CHUNK - 1) / CHUNK; }

static void startCapture() {
  audioLen = 0;
  captureFull = false;
  lastShownSec = -1;
  capturing = true;
}

static void finishRecording() {
  capturing = false;
  delay(5);  // let a chunk in flight on the capture task finish
  if (audioLen < AUDIO_RATE / 10) { audioLen = 0; showResult(2, "Too short"); return; }
  recordId = millis();
  audioSent = 0;
  audioSeq = 0;
  char v[160];
  snprintf(v, sizeof(v), "{\"id\":%lu,\"rate\":%d,\"bits\":8,\"encoding\":\"mulaw\",\"bytes\":%d,\"chunk\":%d}",
           (unsigned long)recordId, AUDIO_RATE, (int)audioLen, CHUNK);
  setState(UPLOADING);
  emit("audio_start", v, 200);
}

static void setPlaces(const char* v) {
  int n = 0;
  const char* p = v;
  char tmp[MAX_PLACES][24];
  while (*p && n < MAX_PLACES) {
    const char* e = strchr(p, ',');
    size_t len = e ? (size_t)(e - p) : strlen(p);
    while (len && *p == ' ') { p++; len--; }
    while (len && p[len - 1] == ' ') len--;
    if (len) { safeCopy(tmp[n], sizeof(tmp[n]), p, len); n++; }
    if (!e) break;
    p = e + 1;
  }
  if (n) {
    memcpy(places, tmp, sizeof(tmp[0]) * n);
    numPlaces = n;
    if (sel >= n) sel = 0;
  }
  char out[24];
  snprintf(out, sizeof(out), "{\"count\":%d}", numPlaces);
  emit("places", out);
  screenDirty = true;
}

// ---------------- host actions ----------------
// Returns 0 = not ours (the SDK answers "unknown action"), 1 = done (ok),
// 2 = failed: *err is set to a static reason string.
static int handleAction(const char* a, const char* value, const char** err) {
  if (!strcmp(a, "voice")) {
    // value: {"name":..,"expiry":..,"quantity":..,"id":..}  or "name|expiry|qty"
    if (!(state == WAIT_VOICE || state == IDLE || state == RESULT)) { *err = "busy"; return 2; }
    char tmp[24];
    char n[sizeof(fName)] = "", e[sizeof(fExpiry)] = "", q[sizeof(fQty)] = "";
    if (value[0] == '{') {
      if (jsonField(value, "id", tmp, sizeof(tmp)) && state == WAIT_VOICE &&
          strtoul(tmp, nullptr, 10) != recordId) { *err = "stale id"; return 2; }
      jsonField(value, "name", n, sizeof(n));
      jsonField(value, "expiry", e, sizeof(e));
      if (!jsonField(value, "quantity", q, sizeof(q))) jsonField(value, "qty", q, sizeof(q));
    } else {
      const char* p1 = strchr(value, '|');
      const char* p2 = p1 ? strchr(p1 + 1, '|') : nullptr;
      if (!p1 || !p2) { *err = "voice needs name|expiry|qty"; return 2; }
      safeCopy(n, sizeof(n), value, p1 - value);
      safeCopy(e, sizeof(e), p1 + 1, p2 - p1 - 1);
      safeCopy(q, sizeof(q), p2 + 1, strlen(p2 + 1));
    }
    if (!n[0]) { *err = "no name"; return 2; }
    if (state != WAIT_VOICE) recordId = millis();  // typed by hand, no recording
    strcpy(fName, n); strcpy(fExpiry, e); strcpy(fQty, q);
    sel = 0;
    setState(MENU);
    return 1;
  }
  if (!strcmp(a, "voice_error")) {
    if (state != WAIT_VOICE) { *err = "not waiting for a transcript"; return 2; }
    showResult(2, value[0] ? value : "Not understood");
    return 1;
  }
  if (!strcmp(a, "saved") || !strcmp(a, "error")) {
    if (state != WAIT_SAVE) { *err = "not waiting for a save"; return 2; }
    char tmp[24];
    if (value[0] == '{' && jsonField(value, "id", tmp, sizeof(tmp)) &&
        strtoul(tmp, nullptr, 10) != recordId) { *err = "stale id"; return 2; }
    if (a[0] == 's') {
      lastSaveKind = 1;
      showResult(1, "Stored in DB");
    } else {
      lastSaveKind = 2;
      char msg[40] = "";
      if (value[0] == '{') jsonField(value, "msg", msg, sizeof(msg));
      else safeCopy(msg, sizeof(msg), value, strlen(value));
      showResult(2, msg[0] ? msg : "Backend error");
    }
    return 1;
  }
  if (!strcmp(a, "audio_resend")) {
    int seq = atoi(value);
    if (audioLen == 0 || seq < 0 || seq >= audioChunkCount()) { *err = "no such chunk"; return 2; }
    if (!sendAudioChunk(seq, 300)) { *err = "usb busy"; return 2; }
    return 1;
  }
  if (!strcmp(a, "places")) { setPlaces(value); return 1; }
  if (!strcmp(a, "limits")) {
    char tmp[16];
    if (jsonField(value, "t_min", tmp, sizeof(tmp))) tMin = atof(tmp);
    if (jsonField(value, "t_max", tmp, sizeof(tmp))) tMax = atof(tmp);
    if (jsonField(value, "h_max", tmp, sizeof(tmp))) hMax = atof(tmp);
    readClimate(true);
    sendClimate("limits_changed");
    return 1;
  }
  if (!strcmp(a, "climate_now")) { readClimate(true); sendClimate("requested"); return 1; }
  if (!strcmp(a, "report_minutes")) {
    int m = atoi(value);
    if (m < 1) { *err = "minutes must be >= 1"; return 2; }
    reportMinutes = m;
    return 1;
  }
  if (!strcmp(a, "knob_reverse")) { knobReverse = !knobReverse; return 1; }
  if (!strcmp(a, "cancel")) { capturing = false; setState(IDLE); return 1; }
  return 0;
}

// ---------------- setup / loop ----------------
static void setup() {
  st7735_tft.setRotation(0);   // portrait: image top toward the board's top edge
  microphone.onAudio(onMicAudio, nullptr);
  rotary_encoder_knob.setAcceleration(false);
  rotary_encoder_knob.setRingColor(0, 120, 255);
  rotary_encoder_knob.setRingBrightness(40);
  button_module.setColor(0, 0, 255);
  button_module.off();
  lastReportMs = millis();
  char v[120];
  snprintf(v, sizeof(v), "{\"app\":\"medtracker\",\"audio_max_s\":%d,\"tx_free\":%d}",
           MAX_TALK_SECONDS, Serial.availableForWrite());
  emit("app_info", v, 200);
}

// Input edges, collected every pass and consumed on the next UI frame.
static bool btnPressed = false, knobPressed = false;
static int knobSteps = 0;
static unsigned long btnUpSince = 0;

static void loop() {
  // rotary_encoder_knob.update() already ran in the generated loop.
  btnPressed |= button_module.wasPressed();
  button_module.wasReleased();  // keep the driver's flag from going stale
  bool btnDown = button_module.isPressed();
  if (btnDown) btnUpSince = 0;
  else if (!btnUpSince) btnUpSince = millis();
  knobPressed |= rotary_encoder_knob.wasPressed();
  if (rotary_encoder_knob.wasRotatedCW()) knobSteps += knobReverse ? -1 : 1;
  if (rotary_encoder_knob.wasRotatedCCW()) knobSteps += knobReverse ? 1 : -1;

  // Stream audio as fast as the USB buffer drains (bounded per pass).
  if (state == UPLOADING) {
    for (int i = 0; i < 16 && audioSent < audioLen; i++) {
      if (!sendAudioChunk(audioSeq, 0)) break;
      audioSeq++;
      audioSent += CHUNK;
      if (audioSent > audioLen) audioSent = audioLen;
    }
    if (millis() - lastUploadDraw > 250) { lastUploadDraw = millis(); screenDirty = true; }
    if (audioSent >= audioLen) {
      char v[96];
      snprintf(v, sizeof(v), "{\"id\":%lu,\"chunks\":%d,\"bytes\":%d}",
               (unsigned long)recordId, audioSeq, (int)audioLen);
      if (emit("audio_end", v, 200)) setState(WAIT_VOICE);
    } else if (millis() - stateSince > UPLOAD_TIMEOUT_MS) {
      showResult(3, "No computer");
    }
  }

  // climate: read every 10 s, report every reportMinutes (and once at start)
  if (climateTimer.ready(10000)) {
    readClimate();
    if (!firstReportDone && !isnan(lastT)) {
      sendClimate("startup");
      firstReportDone = true;
      lastReportMs = millis();
    }
  }
  if (millis() - lastReportMs >= reportMinutes * 60000UL) {
    lastReportMs = millis();
    sendClimate("hourly");
  }

  if (!frameTimer.ready(30)) return;

  switch (state) {
    case IDLE:
    case RESULT:
      if (state == RESULT && millis() - stateSince > RESULT_SHOW_MS) setState(IDLE);
      if (btnPressed && btnDown) {
        lastSaveKind = 0;
        startCapture();
        button_module.on();
        btnUpSince = 0;
        setState(RECORDING);
      }
      break;
    case RECORDING: {
      // records exactly while the button is held (up to MAX_TALK_SECONDS)
      if (audioLen / AUDIO_RATE != lastShownSec) { lastShownSec = audioLen / AUDIO_RATE; screenDirty = true; }
      bool released = btnUpSince && millis() - btnUpSince >= RELEASE_DEBOUNCE_MS;
      if (released || captureFull || !capturing) {
        if (captureFull) {
          char v[24];
          snprintf(v, sizeof(v), "{\"seconds\":%d}", MAX_TALK_SECONDS);
          emit("recording_full", v);
        }
        button_module.off();
        finishRecording();
      }
      break;
    }
    case WAIT_VOICE:
      if (millis() - stateSince > VOICE_TIMEOUT_MS) showResult(3, "No transcript");
      else if (btnPressed) setState(IDLE);
      break;
    case MENU:
      if (knobSteps) {
        sel = atech::wrap(sel + knobSteps, numPlaces);
        screenDirty = true;
      }
      if (knobPressed) sendRecord();  // only the knob press confirms the place
      break;
    case WAIT_SAVE:
      if (millis() - stateSince > SAVE_TIMEOUT_MS) { lastSaveKind = 2; showResult(3, "No reply"); }
      break;
    default:
      break;
  }
  btnPressed = knobPressed = false;
  knobSteps = 0;

  if (screenDirty) { screenDirty = false; drawScreen(); }
  if (ledDirty) { ledDirty = false; drawLeds(); }
}

}  // namespace med
