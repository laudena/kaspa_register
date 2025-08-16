# kaspa_register.py
from flask import Flask, request, render_template
import threading
import time
from urllib.parse import quote_plus
import board, digitalio

# Own the reset line (RSTPD_N, active-low). Keep it OFF at boot.
_rst_pin = digitalio.DigitalInOut(board.D25)
_rst_pin.direction = digitalio.Direction.OUTPUT
_rst_pin.value = False  # hold PN532 in reset so it can't interfere


# Feedback LED on GPIO13: False=OFF, True=ON
_ready_led = digitalio.DigitalInOut(board.D13)
_ready_led.direction = digitalio.Direction.OUTPUT
_ready_led.value = False

# Timer to auto-turn off the LED after a delay
_led_lock = threading.Lock()
_led_timer = None

def _led_off():
    global _led_timer
    with _led_lock:
        _ready_led.value = False
        _led_timer = None

def _led_on_timed(seconds: float = 30.0):
    """Turn LED ON now; auto-OFF after `seconds`."""
    global _led_timer
    with _led_lock:
        if _led_timer is not None:
            _led_timer.cancel()
        _ready_led.value = True
        _led_timer = threading.Timer(seconds, _led_off)
        _led_timer.daemon = True
        _led_timer.start()

# Use your local module exactly like test_ntag_writer.py
from ntag_writer import (
    ACR1252Type2Transport,
    PN532Type2Transport,
    Ntag21xWriter,
    NfcError,
    NDEFWriterError,
)

app = Flask(__name__)

# Default config
config = {
    "address": "kaspa:qqll33tlfscxfyzwp204l06wgtz32yckln5nlpqanmcvk5xgphxpc57sark5n",
    "amount": "5.00",
    "transport": "pn532",      # default 
    "reader_hint": "ACR1252",  # only used for ACR
    "verify": True,
    "assume_present": True,    # <-- glued tag: skip waiting loops
    "poll_timeout": 0.05,      # <-- when we DO wait, keep it short
    "message": "Thanks!!!",
}


_transport_cache = {"pn532": None, "acr": None}
_transport_lock = threading.Lock()

status_lock = threading.Lock() 

status = {
    "running": False,
    "ok": None,         # None until we have a result; True/False afterwards
    "message": "",
    "wrote_uri": "",
    "uid": None,         
    "records": [],      
}

def pn532_enable():
    _ready_led.value = False
    _rst_pin.value = True
    time.sleep(0.1)  # boot
    # ensure transport exists (or reuse)
    t = _transport_cache.get("pn532") or get_transport("pn532")
    # clear any stale selection so we re-select next
    if hasattr(t, "_uid"):
        t._uid = None
    # re-configure (cheap)
    if hasattr(t, "pn") and hasattr(t.pn, "SAM_configuration"):
        try:
            t.pn.SAM_configuration()
        except Exception as e:
            print("⚠️ SAM_configuration after enable failed:", e)

def pn532_rf_field(on: bool):
    """
    Best-effort RF field toggle via PN532 RFConfiguration (0x32, item=0x01).
    Safe to call even if unsupported; will just print a warning or no-op.
    """
    t = _transport_cache.get("pn532")
    if not t or not hasattr(t, "pn"):
        return
    cf = getattr(t.pn, "_call_function", None)  # private in Adafruit lib
    if callable(cf):
        try:
            # 0x32 = RFConfiguration, params: [0x01 (RF Field), 0x00=OFF / 0x01=ON]
            cf(0x32, bytes([0x01, 0x01 if on else 0x00]), response_length=0, timeout=1)
        except Exception as e:
            print("⚠️ RFConfiguration toggle failed:", e)

def pn532_disable():
    # optional: best-effort RF off (harmless if not supported)
    pn532_rf_field(False)
    # hold in reset so RF is definitely off
    _rst_pin.value = False
    # also clear selection to be safe
    t = _transport_cache.get("pn532")
    if t and hasattr(t, "_uid"):
        t._uid = None
    _led_on_timed(20)


# (HTML moved to templates/index.html + templates/base.html)

def get_transport(kind: str):
    with _transport_lock:
        if kind == "pn532":
            if _transport_cache["pn532"] is None:
                _transport_cache["pn532"] = PN532Type2Transport(
                    auto_wait=not config.get("assume_present", False),
                    poll_timeout=float(config.get("poll_timeout", 0.05)),
                    reset=_rst_pin,
                )
            return _transport_cache["pn532"]
        if kind == "acr":
            if _transport_cache["acr"] is None:
                _transport_cache["acr"] = ACR1252Type2Transport(
                    reader_hint=config.get("reader_hint", "ACR1252")
                )
            return _transport_cache["acr"]
        raise ValueError(f"Unknown transport: {kind}")

def write_with_ntag_writer(uri: str, transport: str):
    """
    Fast path with a 1-shot quick select so PN532 binds to the tag:
      - quick wait_for_tag(tries=1) ~ poll_timeout
      - CC read (capacity check)
      - write TLV
      - optional verify (disabled in your config)
    """
    t0 = time.perf_counter()

    # Build/reuse transport & writer
    t_detect0 = time.perf_counter()
    tport = get_transport(transport)
    writer = Ntag21xWriter(tport)

    # Always do a tiny select; poll_timeout is small (e.g., 0.05s)
    try:
        if hasattr(tport, "wait_for_tag"):
            tport.wait_for_tag(tries=1)
    except Exception as e:
        print(f"⚠️ quick select failed: {e}")

    uid_hex = writer.get_uid_hex()

    # Use CC read as the detection/probe
    cc, cap = writer._read_cc()
    if cc[0] != 0xE1:
        raise NfcError("Tag is not NDEF-enabled (CC0 != 0xE1).")
    if cap not in (496, 504, 872, 888):
        raise NfcError(f"Capacity {cap}B not NTAG215/216 (got {cap}).")
    t_detect1 = time.perf_counter()

    # Build TLV
    ndef = writer._ndef_uri_bytes(uri)
    if len(ndef) >= 0xFF:
        tlv = bytearray(b"\x03\xff\x00\x00")
        tlv[2] = (len(ndef) >> 8) & 0xFF
        tlv[3] = (len(ndef) >> 0) & 0xFF
    else:
        tlv = bytearray([0x03, len(ndef)])
    tlv += ndef
    tlv += b"\xFE"
    while len(tlv) % 4:
        tlv += b"\x00"

    first = 4
    last = writer._last_user_page_from_capacity(cap)

    # Timed write
    t_write0 = time.perf_counter()
    for off in range(0, len(tlv), 4):
        page = first + (off // 4)
        if page > last:
            raise NDEFWriterError("Out of user pages while writing TLV.")
        writer.t.write4(page, tlv[off:off+4])
    t_write1 = time.perf_counter()

    # Optional verify (you have verify=False)
    records = []
    t_verify_ms = None
    if config.get("verify", False):
        tv0 = time.perf_counter()
        records = writer.verify()
        tv1 = time.perf_counter()
        t_verify_ms = (tv1 - tv0) * 1000.0

    times = {
        "detect_wait_ms": (t_detect1 - t_detect0) * 1000.0,
        "write_ms":       (t_write1   - t_write0) * 1000.0,
        "verify_ms":      t_verify_ms,
        "total_ms":       ((tv1 if t_verify_ms is not None else t_write1) - t0) * 1000.0,
    }
    return {"uid": uid_hex, "records": records, "times": times}

def start_writer(uri: str, transport: str):
    with status_lock:
        if status["running"]:
            return
        status.update({
            "running": True,
            "ok": None,
            "message": "",
            "wrote_uri": uri,
            "uid": None,
            "records": [],
        })

    def _task():
        ok = False
        msg = ""
        uid = None
        records = []
        try:
            pn532_enable()
            print(f"📝 Starting write via {transport}: {uri}")
            result = write_with_ntag_writer(uri, transport)
            uid = result.get("uid")
            records = result.get("records", [])
            times = result.get("times", {})
            print(
                "⏱ timings (ms): "
                f"detect_wait={times.get('detect_wait_ms',0):.0f}, "
                f"write={times.get('write_ms',0):.0f}, "
                + (f"verify={times['verify_ms']:.0f}, " if times.get('verify_ms') is not None else "verify=skipped, ")
                + f"total={times.get('total_ms',0):.0f}"
            )
            ok = True
            msg = "Done"
        except (NfcError, NDEFWriterError) as e:
            ok = False
            msg = str(e)
        except Exception as e:
            ok = False
            msg = f"Unexpected error: {e}"
        finally:
            pn532_disable()
            with status_lock:
                status.update({
                    "running": False,
                    "ok": ok,
                    "message": msg,
                    "uid": uid,
                    "records": records,
                })
            print(("✅" if ok else "❌"), f"Write result: {msg}")

    threading.Thread(target=_task, daemon=True).start()


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        config["address"]   = request.form.get("address", config["address"]).strip()
        config["amount"]    = request.form.get("amount",  config["amount"]).strip()
        config["message"]   = request.form.get("message", config["message"]).strip()

        # Build the final write URI in the required format:
        # kaspa:... ?amount=1.23&label=Store&message=...
        base = config["address"]
        sep = '&' if '?' in base else '?'
        uri = (
            f'{base}{sep}'
            f'amount={quote_plus(config["amount"])}'
            f'&label={quote_plus("Store")}'
            f'&message={quote_plus(config["message"])}'
        )
        start_writer(uri, config["transport"])

    # Preview should match exactly what we write:
    base = config["address"]
    sep = '&' if '?' in base else '?'
    full_url = (
        f'{base}{sep}'
        f'amount={quote_plus(config["amount"])}'
        f'&label={quote_plus("Store")}'
        f'&message={quote_plus(config["message"])}'
    )

    with status_lock:
        ctx = {
            "running":  status["running"],
            "ok":       status["ok"],
            "message":  status["message"],   # status text (ok/fail details)
            "wrote_uri":status["wrote_uri"],
            "uid":      status["uid"],
            "records":  status["records"],
        }

    # Avoid name collision with the merchant's "message"
    status_message = ctx.pop("message", "")

    return render_template(
        "index.html",
        page_title="Kaspa Point of Sale Control",
        address=config["address"],
        amount=config["amount"],
        message=config["message"],        # merchant message (form field)
        full_url=full_url,
        transport=config["transport"],
        reader_hint=config["reader_hint"],
        status_message=status_message,    # renamed status text
        **ctx,
    )
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
