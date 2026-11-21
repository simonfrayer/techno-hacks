// Medicine intake + storage monitor.
// Hold the button and speak (name, expiry, tablet count) -> audio is sent to the
// backend for speech-to-text -> backend replies "voice" with the fields -> pick a
// storage place with the knob -> record is sent -> backend replies "saved"/"error".
// Temperature/humidity reported hourly; status light shows storage conditions.
#include <Arduino.h>
#include "transport.h"
#include "wifi_gateway.h"
#include "esp_system.h"
#include "pins.h"
#include "modules/shared/atech_helpers.h"
#include "modules/display/st7735_tft.h"
#include "modules/audio/microphone.h"
#include "modules/input/rotary_encoder.h"
#include "modules/input/button.h"
#include "modules/led/neopixel.h"
#include "modules/temp_humidity/aht20.h"
#include "i2c_software.h"

ST7735_TFT st7735_tft(PIN_ST7735_TFT_D, PIN_ST7735_TFT_B, PIN_ST7735_TFT_C, PIN_ST7735_TFT_A);
Microphone microphone(PIN_MICROPHONE_A, PIN_MICROPHONE_B, PIN_MICROPHONE_C, PIN_MICROPHONE_D);
RotaryEncoder rotary_encoder_knob(PIN_ROTARY_ENCODER_KNOB_A, PIN_ROTARY_ENCODER_KNOB_B, PIN_ROTARY_ENCODER_KNOB_C, PIN_ROTARY_ENCODER_KNOB_D);
ButtonModule button_module(PIN_BUTTON_MODULE_A, PIN_BUTTON_MODULE_B);
NeoPixelGrid neopixel_grid(PIN_NEOPIXEL_GRID_A, PIN_NEOPIXEL_GRID_B);
SoftwareI2C aht20_sensor_i2c(PIN_AHT20_SENSOR_SDA, PIN_AHT20_SENSOR_SCL);
AHT20 aht20_sensor(&aht20_sensor_i2c, 0x38);

// ---------------- state ----------------
enum State { IDLE, RECORDING, UPLOADING, WAIT_VOICE, MENU, WAIT_SAVE, RESULT };
State state = IDLE;

static const int AUDIO_RATE = 8000;                     // sent rate (downsampled 2:1)
static const int MAX_TALK_SECONDS = 15;                 // memory limit of one hold
static const int AUDIO_MAX = AUDIO_RATE * MAX_TALK_SECONDS;
static int8_t audioBuf[AUDIO_MAX];
static volatile int audioLen = 0;                       // written by the capture task while recording
static volatile bool capturing = false;
static volatile bool captureFull = false;
static int audioSent = 0, audioSeq = 0;
static int lastShownSec = -1;

// Streaming tap on the microphone's capture task (core 0): records for as long
// as `capturing` is set, 2:1 decimated to 8 kHz, 8-bit. Must stay fast.
static int capCarry = 0; static bool capHasCarry = false;
void IRAM_ATTR onMicAudio(const int16_t* smp, int count, void*) {
  if (!capturing) { capHasCarry = false; return; }
  int len = audioLen;
  for (int i = 0; i < count; i++) {
    if (!capHasCarry) { capCarry = smp[i]; capHasCarry = true; continue; }
    capHasCarry = false;
    if (len >= AUDIO_MAX) { captureFull = true; capturing = false; break; }
    int v = (capCarry + (int)smp[i]) / 2;
    v >>= 6;                                            // gain: keep quiet voice audible in 8 bits
    if (v > 127) v = 127; if (v < -128) v = -128;
    audioBuf[len++] = (int8_t)v;
  }
  audioLen = len;
}
static const int CHUNK = 120;                           // bytes per line -> 160 base64 chars
uint32_t recordId = 0;

char fName[64] = "", fExpiry[24] = "", fQty[12] = "";

#define MAX_PLACES 10
char places[MAX_PLACES][24] = {"Pharmacy shelf A", "Pharmacy shelf B", "Fridge", "Cabinet 1", "Cabinet 2"};
int numPlaces = 5;
int sel = 0;
bool knobReverse = false;

float tMin = 2.0f, tMax = 25.0f, hMax = 60.0f;
float lastT = NAN, lastH = NAN;
int climateLevel = -1;          // 0 ok, 1 near limit, 2 over, -1 unknown
uint32_t reportMinutes = 60;

int resultKind = 0;
int lastSaveKind = 0;           // top row: 0 none, 1 saved (green), 2 not saved (red)             // 1 saved, 2 error, 3 timeout
char resultMsg[40] = "";
unsigned long stateSince = 0;
bool screenDirty = true, ledDirty = true;

EveryTimer frameTimer, chunkTimer, climateTimer, reportTimer;
unsigned long lastReportMs = 0;
bool firstReportDone = false;

// ---------------- helpers ----------------
void setState(State s) { state = s; stateSince = millis(); screenDirty = true; ledDirty = true; }

static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
void b64(const uint8_t* in, int n, char* out) {
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
void safeCopy(char* dst, size_t n, const char* src, size_t len) {
  size_t o = 0;
  for (size_t i = 0; i < len && o + 1 < n; i++) {
    char c = src[i];
    if (c == '"' || c == '\\' || (unsigned char)c < 32) continue;
    dst[o++] = c;
  }
  dst[o] = 0;
}

// extract "key": value (string or number) from a flat JSON object
bool jsonField(const char* js, const char* key, char* out, size_t n) {
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

void sendClimate(const char* reason) {
  if (isnan(lastT) || isnan(lastH)) {
    Serial.printf("{\"type\":\"climate\",\"payload\":{\"ok\":false,\"reason\":\"%s\"}}\n", reason);
    return;
  }
  const char* st = climateLevel == 2 ? "alarm" : climateLevel == 1 ? "warning" : "ok";
  Serial.printf("{\"type\":\"climate\",\"payload\":{\"temp_c\":%.1f,\"humidity\":%.1f,\"status\":\"%s\",\"reason\":\"%s\",\"t_min\":%.1f,\"t_max\":%.1f,\"h_max\":%.1f}}\n",
                lastT, lastH, st, reason, tMin, tMax, hMax);
}

void readClimate() {
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
    ledDirty = true; screenDirty = true;
    if (prev != -1 || lvl > 0) sendClimate("status_change");
  }
}

// ---------------- LEDs: top row = record status, rows 1-2 = storage ----------------
void drawLeds() {
  neopixel_grid.setBrightness(40);
  neopixel_grid.clear();
  uint8_t r = 0, g = 0, b = 0;
  switch (state) {
    case RECORDING: r = 0; g = 0; b = 255; break;
    case UPLOADING: case WAIT_VOICE: case WAIT_SAVE: r = 0; g = 120; b = 255; break;
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
void line(const char* s, int y, uint16_t c, uint8_t size = 1) {
  char buf[16];
  int maxc = size == 1 ? 13 : 6;
  strncpy(buf, s, maxc); buf[maxc] = 0;
  st7735_tft.displayText(buf, 1, y, size, c);
}

void drawScreen() {
  st7735_tft.fillScreen(ST7735_TFT::COLOR_BLACK);
  char b[40];
  // storage strip at the bottom
  if (isnan(lastT)) snprintf(b, sizeof(b), "--.-C --%%");
  else snprintf(b, sizeof(b), "%.1fC %.0f%%", lastT, lastH);
  uint16_t cc = climateLevel == 2 ? ST7735_TFT::COLOR_RED : climateLevel == 1 ? ST7735_TFT::COLOR_ORANGE : ST7735_TFT::COLOR_GREEN;
  if (climateLevel < 0) cc = ST7735_TFT::COLOR_WHITE;
  line(b, 148, cc);

  switch (state) {
    case IDLE:
      line("READY", 4, ST7735_TFT::COLOR_CYAN, 2);
      line("Hold button", 30, ST7735_TFT::COLOR_WHITE);
      line("and say:", 42, ST7735_TFT::COLOR_WHITE);
      line("name,", 58, ST7735_TFT::COLOR_YELLOW);
      line("expiry date,", 70, ST7735_TFT::COLOR_YELLOW);
      line("tablet count", 82, ST7735_TFT::COLOR_YELLOW);
      break;
    case RECORDING:
      line("REC", 4, ST7735_TFT::COLOR_RED, 2);
      line("Speak now...", 30, ST7735_TFT::COLOR_WHITE);
      line("Release when", 50, ST7735_TFT::COLOR_WHITE);
      line("done", 62, ST7735_TFT::COLOR_WHITE);
      snprintf(b, sizeof(b), "%ds / %ds", audioLen / AUDIO_RATE, MAX_TALK_SECONDS);
      line(b, 82, ST7735_TFT::COLOR_YELLOW);
      break;
    case UPLOADING:
      line("SEND", 4, ST7735_TFT::COLOR_CYAN, 2);
      snprintf(b, sizeof(b), "Audio %d%%", audioLen ? (int)(100L * audioSent / audioLen) : 0);
      line(b, 30, ST7735_TFT::COLOR_WHITE);
      break;
    case WAIT_VOICE:
      line("WAIT", 4, ST7735_TFT::COLOR_CYAN, 2);
      line("Understanding", 30, ST7735_TFT::COLOR_WHITE);
      line("your voice...", 42, ST7735_TFT::COLOR_WHITE);
      break;
    case MENU: {
      line(fName, 2, ST7735_TFT::COLOR_YELLOW);
      snprintf(b, sizeof(b), "Exp %s", fExpiry); line(b, 12, ST7735_TFT::COLOR_WHITE);
      snprintf(b, sizeof(b), "Qty %s", fQty);    line(b, 22, ST7735_TFT::COLOR_WHITE);
      line("Place:", 36, ST7735_TFT::COLOR_CYAN);
      int first = sel - 3; if (first < 0) first = 0;
      if (first > numPlaces - 8) first = numPlaces - 8 < 0 ? 0 : numPlaces - 8;
      for (int i = first, row = 0; i < numPlaces && row < 8; i++, row++) {
        int y = 48 + row * 12;
        if (i == sel) {
          st7735_tft.fillRect(0, y - 2, 80, 11, ST7735_TFT::COLOR_BLUE);
          line(places[i], y, ST7735_TFT::COLOR_WHITE);
        } else line(places[i], y, 0x8410);
      }
      break;
    }
    case WAIT_SAVE:
      line("SAVE", 4, ST7735_TFT::COLOR_CYAN, 2);
      line("Saving to", 30, ST7735_TFT::COLOR_WHITE);
      line(places[sel], 42, ST7735_TFT::COLOR_YELLOW);
      break;
    case RESULT:
      if (resultKind == 1) line("SAVED", 4, ST7735_TFT::COLOR_GREEN, 2);
      else { line("TRY", 4, ST7735_TFT::COLOR_RED, 2); line("AGAIN", 22, ST7735_TFT::COLOR_RED, 2); }
      line(resultMsg, 44, ST7735_TFT::COLOR_WHITE);
      line(fName, 58, ST7735_TFT::COLOR_YELLOW);
      break;
  }
  st7735_tft.display();
}

void showResult(int kind, const char* msg) {
  resultKind = kind;
  safeCopy(resultMsg, sizeof(resultMsg), msg, strlen(msg));
  setState(RESULT);
}

void sendRecord() {
  Serial.printf("{\"type\":\"record\",\"payload\":{\"id\":%lu,\"name\":\"%s\",\"expiry\":\"%s\",\"quantity\":\"%s\",\"place\":\"%s\"}}\n",
                (unsigned long)recordId, fName, fExpiry, fQty, places[sel]);
  setState(WAIT_SAVE);
}

void setPlaces(const char* v) {
  int n = 0; const char* p = v;
  while (*p && n < MAX_PLACES) {
    const char* e = strchr(p, ',');
    size_t len = e ? (size_t)(e - p) : strlen(p);
    while (len && *p == ' ') { p++; len--; }
    if (len) { safeCopy(places[n], sizeof(places[n]), p, len); n++; }
    if (!e) break;
    p = e + 1;
  }
  if (n) { numPlaces = n; if (sel >= n) sel = 0; }
  Serial.printf("{\"type\":\"places\",\"payload\":{\"count\":%d}}\n", numPlaces);
  screenDirty = true;
}

void handleCommand(const P2PCommand& c) {
  const char* a = c.action;
  if (!strcmp(a, "voice")) {
    // value: {"name":..,"expiry":..,"quantity":..}  or "name|expiry|qty"
    char tmp[64];
    if (c.value[0] == '{') {
      fName[0] = fExpiry[0] = fQty[0] = 0;
      jsonField(c.value, "name", fName, sizeof(fName));
      jsonField(c.value, "expiry", fExpiry, sizeof(fExpiry));
      if (!jsonField(c.value, "quantity", fQty, sizeof(fQty))) jsonField(c.value, "qty", fQty, sizeof(fQty));
      if (jsonField(c.value, "id", tmp, sizeof(tmp))) recordId = strtoul(tmp, nullptr, 10);
    } else {
      const char* p1 = strchr(c.value, '|');
      const char* p2 = p1 ? strchr(p1 + 1, '|') : nullptr;
      if (!p1 || !p2) { Serial.println("{\"type\":\"error\",\"payload\":{\"msg\":\"voice needs name|expiry|qty\"}}"); return; }
      safeCopy(fName, sizeof(fName), c.value, p1 - c.value);
      safeCopy(fExpiry, sizeof(fExpiry), p1 + 1, p2 - p1 - 1);
      safeCopy(fQty, sizeof(fQty), p2 + 1, strlen(p2 + 1));
      if (state != WAIT_VOICE) recordId = millis();
    }
    if (!fName[0]) { showResult(2, "Not understood"); return; }
    sel = 0;
    setState(MENU);
  } else if (!strcmp(a, "voice_error")) {
    showResult(2, c.value[0] ? c.value : "Not understood");
  } else if (!strcmp(a, "saved")) {
    if (state == WAIT_SAVE) { lastSaveKind = 1; showResult(1, "Stored in DB"); }
  } else if (!strcmp(a, "error")) {
    if (state == WAIT_SAVE) lastSaveKind = 2;
    showResult(2, c.value[0] ? c.value : "Backend error");
  } else if (!strcmp(a, "places")) {
    setPlaces(c.value);
  } else if (!strcmp(a, "limits")) {
    char tmp[16];
    if (jsonField(c.value, "t_min", tmp, sizeof(tmp))) tMin = atof(tmp);
    if (jsonField(c.value, "t_max", tmp, sizeof(tmp))) tMax = atof(tmp);
    if (jsonField(c.value, "h_max", tmp, sizeof(tmp))) hMax = atof(tmp);
    climateLevel = -2;  // force re-evaluation + report
    readClimate();
    sendClimate("limits_changed");
  } else if (!strcmp(a, "climate_now")) {
    readClimate(); sendClimate("requested");
  } else if (!strcmp(a, "report_minutes")) {
    int m = atoi(c.value); if (m >= 1) reportMinutes = m;
  } else if (!strcmp(a, "knob_reverse")) {
    knobReverse = !knobReverse;
  } else if (!strcmp(a, "cancel")) {
    setState(IDLE);
  } else if (!strcmp(a, "help")) {
    Serial.println("{\"type\":\"help\",\"payload\":{\"commands\":\"voice, voice_error, saved, error, places, limits, climate_now, report_minutes, knob_reverse, cancel\"}}");
  }
}

void startCapture() {
  audioLen = 0; captureFull = false; lastShownSec = -1;
  capturing = true;
}

void finishRecording() {
  capturing = false;
  delay(5);                                           // let a chunk in flight finish
  if (audioLen < AUDIO_RATE / 10) { audioLen = 0; showResult(2, "Too short"); return; }
  recordId = millis();
  audioSent = 0; audioSeq = 0;
  Serial.printf("{\"type\":\"audio_start\",\"payload\":{\"id\":%lu,\"rate\":%d,\"bits\":8,\"encoding\":\"pcm_s8\",\"bytes\":%d}}\n",
                (unsigned long)recordId, AUDIO_RATE, audioLen);
  setState(UPLOADING);
}

void setup() {
  Serial.setRxBufferSize(8192); Serial.begin(115200); Serial.setTxTimeoutMs(0);
  p2p_transport_setup();
  aht20_sensor_i2c.begin(100000);
  st7735_tft.begin();
  st7735_tft.setRotation(0);   // portrait: image top toward the board's top edge
  microphone.begin();
  rotary_encoder_knob.begin();
  button_module.begin();
  neopixel_grid.begin();
  aht20_sensor.begin();
  microphone.onAudio(onMicAudio, nullptr);

  rotary_encoder_knob.setAcceleration(false);
  rotary_encoder_knob.setRingColor(0, 120, 255);
  rotary_encoder_knob.setRingBrightness(40);
  button_module.setColor(0, 0, 255);
  button_module.off();
  lastReportMs = millis();

  const char* rr = "UNKNOWN";
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: rr = "POWERON"; break;
    case ESP_RST_EXT: rr = "EXT"; break;
    case ESP_RST_SW: rr = "SOFTWARE"; break;
    case ESP_RST_PANIC: rr = "PANIC"; break;
    case ESP_RST_INT_WDT: rr = "INT_WDT"; break;
    case ESP_RST_TASK_WDT: rr = "TASK_WDT"; break;
    case ESP_RST_WDT: rr = "WDT"; break;
    case ESP_RST_BROWNOUT: rr = "BROWNOUT"; break;
    case ESP_RST_DEEPSLEEP: rr = "DEEPSLEEP"; break;
    default: break;
  }
  Serial.printf("{\"type\":\"boot\",\"payload\":{\"reset_reason\":\"%s\",\"free_heap\":%u,\"modules\":["
                "{\"instance\":\"st7735_tft\",\"module\":\"st7735_tft\",\"ok\":true,\"detectable\":false},"
                "{\"instance\":\"microphone\",\"module\":\"microphone\",\"ok\":true,\"detectable\":false},"
                "{\"instance\":\"rotary_encoder_knob\",\"module\":\"rotary_encoder_knob\",\"ok\":true,\"detectable\":false},"
                "{\"instance\":\"button_module\",\"module\":\"button_module\",\"ok\":true,\"detectable\":false},"
                "{\"instance\":\"neopixel_grid\",\"module\":\"neopixel_grid\",\"ok\":true,\"detectable\":false},"
                "{\"instance\":\"aht20_sensor\",\"module\":\"aht20_sensor\",\"ok\":true,\"detectable\":false}]}}\n",
                rr, (unsigned)ESP.getFreeHeap());
}

bool btnDown = false, btnPressed = false, btnReleased = false, knobPressed = false;
int knobSteps = 0;

void loop() {
  p2p_transport_loop();

  P2PCommand cmd;
  while (p2p_next_command(&cmd)) handleCommand(cmd);

  button_module.update();
  rotary_encoder_knob.update();
  btnPressed |= button_module.wasPressed();
  btnReleased |= button_module.wasReleased();
  btnDown = button_module.isPressed();
  knobPressed |= rotary_encoder_knob.wasPressed();
  if (rotary_encoder_knob.wasRotatedCW()) knobSteps += knobReverse ? -1 : 1;
  if (rotary_encoder_knob.wasRotatedCCW()) knobSteps += knobReverse ? 1 : -1;

  // paced audio upload: one line per 55 ms keeps under the gateway's line rate
  if (state == UPLOADING && chunkTimer.ready(55)) {
    int n = audioLen - audioSent; if (n > CHUNK) n = CHUNK;
    char enc[CHUNK / 3 * 4 + 8];
    b64((const uint8_t*)audioBuf + audioSent, n, enc);
    Serial.printf("{\"type\":\"audio\",\"payload\":{\"id\":%lu,\"seq\":%d,\"d\":\"%s\"}}\n", (unsigned long)recordId, audioSeq++, enc);
    audioSent += n;
    if ((audioSeq % 10) == 0) screenDirty = true;
    if (audioSent >= audioLen) {
      Serial.printf("{\"type\":\"audio_end\",\"payload\":{\"id\":%lu,\"chunks\":%d}}\n", (unsigned long)recordId, audioSeq);
      setState(WAIT_VOICE);
    }
  }

  // climate: read every 10 s, report every reportMinutes (and once at start)
  if (climateTimer.ready(10000)) {
    readClimate();
    if (!firstReportDone && !isnan(lastT)) { sendClimate("startup"); firstReportDone = true; lastReportMs = millis(); }
  }
  if (millis() - lastReportMs >= reportMinutes * 60000UL) { lastReportMs = millis(); sendClimate("hourly"); }

  if (!frameTimer.ready(30)) { delay(1); return; }

  switch (state) {
    case IDLE: case RESULT:
      if (state == RESULT && millis() - stateSince > 4000) setState(IDLE);
      if (btnPressed && btnDown) { lastSaveKind = 0; startCapture(); button_module.on(); setState(RECORDING); }
      break;
    case RECORDING:
      // records exactly while the button is held (up to the memory limit)
      if (audioLen / AUDIO_RATE != lastShownSec) { lastShownSec = audioLen / AUDIO_RATE; screenDirty = true; }
      if (!btnDown || btnReleased || captureFull || !capturing) {
        if (captureFull) Serial.printf("{\"type\":\"recording_full\",\"payload\":{\"seconds\":%d}}\n", MAX_TALK_SECONDS);
        button_module.off();
        finishRecording();
      }
      break;
    case WAIT_VOICE:
      if (millis() - stateSince > 20000) showResult(3, "No transcript");
      if (btnPressed) setState(IDLE);
      break;
    case MENU:
      if (knobSteps) {
        sel = atech::wrap(sel + knobSteps, numPlaces);
        screenDirty = true;
      }
      if (knobPressed) sendRecord();   // only the knob press confirms the place
      break;
    case WAIT_SAVE:
      if (millis() - stateSince > 15000) { lastSaveKind = 2; showResult(3, "No reply"); }
      break;
    default: break;
  }
  btnPressed = btnReleased = knobPressed = false;
  knobSteps = 0;

  if (screenDirty) { screenDirty = false; drawScreen(); }
  if (ledDirty) { ledDirty = false; drawLeds(); }
}
