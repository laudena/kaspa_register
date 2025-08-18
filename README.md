# Kaspa Register (NFC Point of Sale)

A small web app that writes Kaspa payment request URIs to NTAG21x NFC tags (Type 2) using either a PN532 (Raspberry Pi via SPI) or an ACR1252 (PC/SC) reader. It provides a simple UI to enter an amount in AUD and an optional message, converts to KAS using the current rate, and writes a Kaspa URI to the tag.

- Frontend: Bootstrap UI with live status, toasts, and rate display.
- Hardware: PN532 over SPI with reset/LED GPIO, or ACR1252 via PC/SC.
- Tags: NTAG215/216 (Type 2 NDEF-Enabled).


## Features
- Request UI with AUD→KAS conversion (Coingecko) and preview.
- One-click write to NFC tag; optional verify & record decode.
- Live status via SSE and lightweight polling endpoints.
- Admin page to set/persist your Kaspa address (`settings.json`).
- PN532 reset and a status LED controlled via GPIO (Pi).


## Repository Layout
- `kaspa_register.py` — Flask app, routes, SSE, GPIO control, write flow.
- `ntag_writer.py` — NTAG21x writer + transports for PN532 and ACR1252.
- `templates/` — Jinja templates (`index.html`, `admin.html`, partials).
- `static/` — CSS and images (Bootstrap loaded from CDN).
- `requirements.txt` — Python dependencies (see note below).
- `test_nfc.py` — Minimal PC/SC reader check for ACR1252.


## Hardware
- PN532 (Adafruit or compatible), default wiring on Raspberry Pi:
  - SPI with CS on `GPIO5` (`board.D5`), RESET on `GPIO25` (`board.D25`).
  - Optional status LED on `GPIO13` (`board.D13`).
- ACR1252 USB reader (PC/SC).
- NFC tags: NTAG215/216 (Type 2, NDEF-enabled).


## Prerequisites
- Python 3.9+ recommended.
- On Raspberry Pi (PN532 over SPI):
  - Enable SPI (`raspi-config`) and reboot; ensure user in `spi` group.
  - System packages you often need: `sudo apt-get install -y python3-dev libffi-dev libusb-1.0-0-dev`.
- For ACR1252 (PC/SC):
  - Linux: `sudo apt-get install -y pcscd pcsc-tools libpcsclite-dev` and start `pcscd`.
  - macOS/Windows: PC/SC is built-in; install a driver if required by your reader.


## Setup
```
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Environment configuration (recommended):
- `KASPA_SECRET`: Flask secret key (default `change-me`).
- `KASPA_ADMIN_PASSWORD`: Basic auth password for `/admin` (default `admin`).

Example:
```
export KASPA_SECRET='something-random'
export KASPA_ADMIN_PASSWORD='strong-password'
```


## Run
- Raspberry Pi + PN532: connect hardware, then:
```
python kaspa_register.py
```
- ACR1252 on desktop: plug the reader, ensure PC/SC is running, then:
```
python kaspa_register.py
```

Open http://localhost:5000 in your browser.


## Usage
1. On `/` enter AUD amount and a message. The app converts to KAS when the rate is available.
2. Click “Request … Kaspa” to start a write. Bring an NTAG215/216 tag to the reader (even better - glue it to the reader)
3. Watch the Status panel for progress and success/failure. Admin page shows fuller details.
4. The written URI format is: `kaspa:<address>?amount=<KAS>&label=<message>&message=<message>`.

Admin page `/admin` (basic auth) lets you set the destination address, persisted in `settings.json` at the repo root.


## Configuration Details
- Address: stored in `settings.json`. Initial default is embedded; update via `/admin`.
- Transport:
  - PN532 (default) uses SPI, does a quick tag select, writes TLV, optional verify.
  - ACR1252 uses PC/SC transparent exchange for Type 2 READ/WRITE.
- Verification: controlled in code via `config["verify"]` (default False).
- Conversion rate: fetched from Coingecko every 10 minutes; if unavailable, the app uses the entered value as KAS directly for writing and shows `…` in preview.


## Endpoints
- `/` — Main POS UI.
- `/admin` — Admin UI (basic auth via `KASPA_ADMIN_PASSWORD`).
- `/status.json` — JSON status snapshot.
- `/status_panel` — HTML status partial (polling).
- `/events` — Server-Sent Events stream of status HTML.
- `/rate.json` — Current AUD per KAS and timestamp.


## Troubleshooting
- “No NFC tag detected”: ensure the tag is Type 2 (NTAG215/216) and within range.
- PN532 on Pi: verify SPI is enabled and wiring matches defaults; run as a user in `spi` group.
- GPIO import errors on non-Pi: the app is intended for Raspberry Pi (uses Blinka `board`/`digitalio`). Use a Pi when using PN532. For desktop ACR1252-only use, you may still need Blinka installed to satisfy imports.
- ACR1252: ensure `pcscd` is running; try `pcsc_scan` to confirm the reader appears. Use `test_nfc.py` to list readers.


## Notes
- The UI uses Bootstrap CDN; an internet connection improves styling and rate fetches, but writing still works offline using the explicit amount as KAS.
- The LED on `GPIO13` lights during activity and auto-turns off after a short delay.
- Default PN532 reset is controlled on `GPIO25`. Adjust wiring in `kaspa_register.py` if needed.


## Development
- Code style is simple and self-contained; no build step.
- To tweak the write flow or verification behavior, see `write_with_ntag_writer` in `kaspa_register.py` and `Ntag21xWriter` in `ntag_writer.py`.

