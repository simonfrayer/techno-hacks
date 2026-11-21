# techno-hacks: medicine intake + storage monitor

Firmware for an Atech board (ESP32-S3). It records spoken medicine details, has a
backend transcribe them, lets the user pick a storage place, and stores the
record. It also watches the temperature and humidity of the storage area.

## Repo layout

- `src/techno_hacks/main.cpp` is the firmware. It was exported from the
  atech.dev web interface, where it compiles. **It has not run on a device yet.**
  The headers it includes (`transport.h`, `wifi_gateway.h`, `pins.h`, `modules/...`)
  come from the atech build and are not in this repo.
- The Python environment is managed with uv (`pyproject.toml`, `.venv/`) and has the
  `atech` SDK (alpha, `1.0.0a12`) installed. Use `uv run atech ...` or
  `source .venv/bin/activate`. Run `uv run atech guide` for the SDK workflow and
  `uv run atech guide protocol` for the wire format.
- There is no `project.yaml` yet.

## Hardware (module instances in main.cpp)

| Instance | Module | Role |
|---|---|---|
| `button_module` | button | Hold to record (its LED is on while recording) |
| `microphone` | microphone | Voice capture |
| `rotary_encoder_knob` | rotary encoder | Turn to choose a storage place, press to confirm |
| `st7735_tft` | 80x160 TFT, portrait | UI |
| `neopixel_grid` | NeoPixel grid | Status light |
| `aht20_sensor` | AHT20 (software I2C, 0x38) | Temperature and humidity |

## Intended user flow

1. **Hold the button** and say the product name, expiry date and tablet count.
   Recording runs **as long as the button is held, up to 15 s**
   (`MAX_TALK_SECONDS`). Letting go stops it. Anything under 0.1 s shows
   "Too short".
2. The audio goes over WiFi to the backend (state `UPLOADING`, then `WAIT_VOICE`).
3. When the backend returns the three details, the screen shows them above the
   list of storage places (`MENU`).
4. **Turn the knob** to choose a place. **Press the knob** to confirm. The button
   does **not** confirm.
5. The record goes to the backend (`WAIT_SAVE`).
   - If it is saved, the screen shows **SAVED** and the top light row turns **green**.
   - If it fails, or there is no reply within **15 s**, the screen shows
     **TRY AGAIN** and the top row turns **red**.
   - The top-row colour stays until the next recording starts. The result screen
     goes back to READY after 4 s.
6. Temperature and humidity are reported every hour (and once at startup, and
   whenever the status changes).

Other behaviour in the code:
- There is a 20 s timeout waiting for the transcript ("No transcript").
- Pressing the button while waiting for the transcript cancels back to idle.
- A `voice_error` result shows TRY AGAIN but does not change the top-row colour.
  Only a save result does.

## Status light (NeoPixel grid)

- **Row 0** shows the record status: blue while recording, light blue while
  uploading or waiting, then green (saved) or red (not saved) until the next
  recording.
- **Rows 1–2** show the storage climate: green is fine, orange is near a limit,
  red is over a limit, grey is no reading.

## Climate limits

- Defaults: **2–25 °C**, humidity **at most 60 %**. They can be changed with the
  `limits` command.
- "Near a limit" means within **1 °C** of either temperature limit, or within
  **5 points** of the humidity limit.
- The sensor is read every 10 s. The report interval defaults to 60 min and can be
  changed with `report_minutes`.

## Backend protocol (JSON lines over the atech WiFi gateway / serial)

**Board → backend** (`{"type": ..., "payload": {...}}`):
- `audio_start` `{id, rate: 8000, bits: 8, encoding: "pcm_s8", bytes}`
- `audio` `{id, seq, d}`, where `d` is base64 of 120 bytes of PCM. One line is sent
  every 55 ms.
- `audio_end` `{id, chunks}`
- `recording_full` `{seconds}`, sent when the 15 s limit is hit.
- `record` `{id, name, expiry, quantity, place}`
- `climate` `{temp_c, humidity, status: ok|warning|alarm, reason, t_min, t_max, h_max}`
  The reason is one of `startup`, `hourly`, `status_change`, `limits_changed` or
  `requested`.
- Also: `boot`, `places`, `help`, `error`.

**Backend → board** (actions):
- `voice`: the name, expiry and count it heard. The value is
  `{"name","expiry","quantity"|"qty","id"?}` or `"name|expiry|qty"`.
- `voice_error`: the backend could not understand the recording. The value is an
  optional message.
- `saved` / `error`: whether the record was stored.
- Configuration: `places` (comma-separated, at most 10 entries of at most 23
  characters each), `limits` `{t_min,t_max,h_max}`, `climate_now`,
  `report_minutes`, `knob_reverse`, `cancel`, `help`.

Default places: Pharmacy shelf A, Pharmacy shelf B, Fridge, Cabinet 1, Cabinet 2.

## Things to watch when testing on the device

- A full 15 s recording is 120,000 bytes. At 120 bytes every 55 ms that is about
  1,000 lines and **about 55 s of upload**, so long recordings are slow to send.
- `audioBuf` is a 120 KB static buffer. Check the free heap in the `boot` message.
