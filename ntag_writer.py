# ntag_writer.py
# Unified NTAG215/216 URI writer with pluggable transports:
# - ACR1252 (PC/SC, pyscard)
# - PN532 (Adafruit CircuitPython PN532, I2C/SPI/UART)
#
# Install:
#   pip install pyscard
#   pip install adafruit-circuitpython-pn532  (only if using PN532)
#
# Usage (see test_ntag_writer.py for a runnable example)

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

# -------- Exceptions --------

class NDEFWriterError(Exception): ...
class NfcError(NDEFWriterError): ...
class TagNotSupported(NDEFWriterError): ...
class CapacityError(NDEFWriterError): ...
class VerificationError(NDEFWriterError): ...


# ============================================================
# ACR1252 Transport (PC/SC via pyscard)
# ============================================================


# ============================================================
# PN532 Transport (Adafruit CircuitPython PN532)
# ============================================================
# ============================================================
# Transport Interface (Type 2)
# ============================================================

class Type2Transport(ABC):
    """Minimal interface for Type-2 tag transceive: READ16 and WRITE4."""
    @abstractmethod
    def read16(self, first_page: int) -> bytes:
        """READ (0x30): return 16 bytes starting at first_page (4 pages)."""
        raise NotImplementedError

    @abstractmethod
    def write4(self, page: int, data4: bytes) -> None:
        """WRITE (0xA2): write exactly 4 bytes to page."""
        raise NotImplementedError

    def get_uid(self) -> Optional[bytes]:
        """Optional: return UID bytes if the transport can provide it."""
        return None
# ===========================================================
class ACR1252Type2Transport(Type2Transport):
    """
    ACR1252 Transparent Exchange wrapper for Type-2 commands.
    Uses PC/SC (pyscard). The reader handles CRC/parity/timeouts.
    """

    _ACS_DO_PARAMS = bytes([0x5F, 0x46, 0x04, 0x40, 0x42, 0x0F, 0x00])  # RF/proto params

    def __init__(self, reader_hint: Optional[str] = "ACR1252"):
        try:
            from smartcard.System import readers
        except Exception as e:
            raise NfcError("pyscard is required for ACR1252 transport") from e

        rs = readers()
        if not rs:
            raise NfcError("No PC/SC readers found.")

        self._reader = None
        if reader_hint:
            for rd in rs:
                name = str(rd)
                if reader_hint.lower() in name.lower() and "PICC" in name:
                    self._reader = rd
                    break
            if self._reader is None:
                for rd in rs:
                    if reader_hint.lower() in str(rd).lower():
                        self._reader = rd
                        break
        if self._reader is None:
            self._reader = rs[0]

        self._conn = self._reader.createConnection()
        self._conn.connect()

    def _transparent_exchange(self, cmd_bytes: bytes) -> bytes:
        """
        FF C2 00 01 <Lc> [5F 46 ...] [95 <len cmd> <cmd...>] -> parse 0x97 'ICC response' DO
        """
        from smartcard.util import toHexString
        DO_params = self._ACS_DO_PARAMS
        DO_cmd    = bytes([0x95, len(cmd_bytes)]) + bytes(cmd_bytes)
        body = DO_params + DO_cmd
        apdu = [0xFF, 0xC2, 0x00, 0x01, len(body)] + list(body)
        resp, sw1, sw2 = self._conn.transmit(apdu)
        if (sw1, sw2) != (0x90, 0x00):
            raise NfcError(f"Transparent Exchange failed: SW={hex(sw1)} {hex(sw2)} Resp={toHexString(resp)}")
        # Parse TLV-like DOs to get 0x97
        b = bytes(resp); i = 0; card = b""
        while i + 2 <= len(b):
            tag = b[i]; i += 1
            ln  = b[i]; i += 1
            val = b[i:i+ln]; i += ln
            if tag == 0x97:
                card = bytes(val)
        return card

    def _end_session(self):
        apdu = [0xFF, 0xC2, 0x00, 0x00, 0x02, 0x82, 0x00]
        try:
            self._conn.transmit(apdu)
        except Exception:
            pass

    def read16(self, first_page: int) -> bytes:
        try:
            r = self._transparent_exchange(bytes([0x30, first_page]))
        finally:
            self._end_session()
        if len(r) != 16:
            raise NfcError(f"ACR1252 READ returned {len(r)} bytes (expected 16)")
        return r

    def write4(self, page: int, data4: bytes) -> None:
        if len(data4) != 4:
            raise ValueError("WRITE needs exactly 4 bytes")
        try:
            _ = self._transparent_exchange(bytes([0xA2, page]) + bytes(data4))
        finally:
            self._end_session()

    def get_uid(self) -> Optional[bytes]:
        try:
            resp, sw1, sw2 = self._conn.transmit([0xFF,0xCA,0x00,0x00,0x00])
            if (sw1, sw2) == (0x90, 0x00):
                return bytes(resp)
        except Exception:
            pass
        return None


# ============================================================
# PN532 Transport (Adafruit CircuitPython PN532)
# ============================================================


class PN532Type2Transport(Type2Transport):
    """
    PN532 transport using Adafruit CircuitPython PN532.
    Default is SPI on Raspberry Pi with CS=D5 and RESET=D25,
    but you can pass a pre-built PN532 object or your own I2C/SPI handles.
    """
    def __init__(self, pn=None, *, i2c=None, spi=None, cs=None, irq=None, reset=None,
                 auto_wait=True, poll_timeout=0.5):
        try:
            import adafruit_pn532.i2c as pn532_i2c
            import adafruit_pn532.spi as pn532_spi
            import busio, board, digitalio
        except Exception as e:
            raise NfcError("adafruit-circuitpython-pn532 is required for PN532 transport") from e

        if pn is not None:
            self.pn = pn
        else:
            if i2c is not None:
                self.pn = pn532_i2c.PN532_I2C(i2c, irq=irq, reset=reset, debug=False)
            elif spi is not None and cs is not None:
                self.pn = pn532_spi.PN532_SPI(spi, cs, reset=reset, debug=False)
            else:
                # DEFAULT: SPI with CS on GPIO5 (board.D5), optional reset on GPIO25 (board.D25)
                spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
                cs_pin = digitalio.DigitalInOut(board.D5) if cs is None else cs
                rst_pin = digitalio.DigitalInOut(board.D25) if reset is None else reset
                self.pn = pn532_spi.PN532_SPI(spi, cs_pin, reset=rst_pin, debug=False)

        self.pn.SAM_configuration()
        self._poll_timeout = poll_timeout
        self._uid = None
        if auto_wait:
            self.wait_for_tag()

    def wait_for_tag(self, tries: int = 40) -> bytes:
        for _ in range(tries):
            uid = self.pn.read_passive_target(timeout=self._poll_timeout)
            if uid:
                self._uid = bytes(uid)
                return self._uid
        raise NfcError("No NFC tag detected (PN532).")

    def read16(self, first_page: int) -> bytes:
        """
        Read 16 bytes starting at 'first_page' using PN532 helpers.
        Tries ntag2xx_read_block (preferred), then mifare_ultralight_read_page.
        """
        # Preferred in recent adafruit-circuitpython-pn532
        if hasattr(self.pn, "ntag2xx_read_block"):
            out = bytearray()
            for p in range(first_page, first_page + 4):
                r = self.pn.ntag2xx_read_block(p)
                if r is None or len(r) != 4:
                    raise TagNotSupported("ntag2xx_read_block failed — likely not a Type-2/NTAG tag.")
                out += r
            return bytes(out)

        # Older / alternate API name
        if hasattr(self.pn, "mifare_ultralight_read_page"):
            out = bytearray()
            for p in range(first_page, first_page + 4):
                r = self.pn.mifare_ultralight_read_page(p)
                if r is None or len(r) != 4:
                    raise TagNotSupported("Ultralight read failed — likely not a Type-2/NTAG tag.")
                out += r
            return bytes(out)

        # No supported helpers found
        raise NfcError(
            "PN532 driver has neither ntag2xx_read_block nor mifare_ultralight_read_page. "
            "Upgrade the library: pip install --upgrade adafruit-circuitpython-pn532"
        )

    def write4(self, page: int, data4: bytes) -> None:
        """
        Write 4 bytes to 'page' using PN532 helpers.
        Tries ntag2xx_write_block, then mifare_ultralight_write_page.
        """
        if len(data4) != 4:
            raise ValueError("WRITE needs exactly 4 bytes")

        if hasattr(self.pn, "ntag2xx_write_block"):
            ok = self.pn.ntag2xx_write_block(page, bytes(data4))
            if ok is False:
                raise NfcError("PN532 ntag2xx_write_block failed")
            return

        if hasattr(self.pn, "mifare_ultralight_write_page"):
            ok = self.pn.mifare_ultralight_write_page(page, bytes(data4))
            if ok is False:
                raise NfcError("PN532 Ultralight write failed")
            return

        raise NfcError(
            "PN532 driver has neither ntag2xx_write_block nor mifare_ultralight_write_page. "
            "Upgrade the library: pip install --upgrade adafruit-circuitpython-pn532"
        )


    def get_uid(self) -> Optional[bytes]:
        return self._uid

# ============================================================
# NTAG21x Writer (uses any Type2Transport)
# ============================================================

class Ntag21xWriter:
    """
    High-level writer for NTAG215/216 (Type-2) that writes a single URI NDEF.
    Works with any Type2Transport (ACR1252Type2Transport or PN532Type2Transport).
    """

    def __init__(self, transport: Type2Transport):
        self.t = transport

    # ---------- Helpers ----------

    @staticmethod
    def _pad4(b: bytes) -> bytes:
        r = len(b) % 4
        return b if r == 0 else b + b"\x00" * (4 - r)

    @staticmethod
    def _ndef_uri_bytes(uri: str) -> bytes:
        prefixes = ["","http://www.","https://www.","http://","https://","tel:","mailto:",
                    "ftp://anonymous:anonymous@","ftp://ftp.","ftps://","sftp://","smb://",
                    "nfs://","ftp://","dav://","news:","telnet://","imap:","rtsp://","urn:",
                    "pop:","sip:","sips:","tftp:","btspp://","btl2cap://","btgoep://","tcpobex://",
                    "irdaobex://","file://","urn:epc:id:","urn:epc:tag:","urn:epc:pat:","urn:epc:raw:",
                    "urn:epc:","urn:nfc:"]
        pidx, best = 0, ""
        for i, p in enumerate(prefixes):
            if p and uri.startswith(p) and len(p) > len(best):
                pidx, best = i, p
        tail = uri[len(best):].encode("utf-8")
        payload = bytes([pidx]) + tail
        t = b"U"
        # Use Short Record (SR) only if payload < 256; otherwise 4-byte length
        if len(payload) < 256:
            header = 0xD1  # MB|ME|SR + TNF=1 (well-known)
            return bytes([header, len(t), len(payload)]) + t + payload
        else:
            header = 0xC1  # MB|ME (no SR) + TNF=1
            plen = len(payload).to_bytes(4, "big")
            return bytes([header, len(t)]) + plen + t + payload

    @staticmethod
    def _parse_ndef_records(msg: bytes) -> List[str]:
        out: List[str] = []

        # --- Heuristic fallback: buffer starts at payload_len instead of header ---
        # Pattern: [len][0x55 'U'][prefix][uri...]
        if len(msg) >= 3 and msg[1] == 0x55 and msg[0] == (len(msg) - 2):
            prefs = ["","http://www.","https://www.","http://","https://","tel:","mailto:",
                     "ftp://anonymous:anonymous@","ftp://ftp.","ftps://","sftp://","smb://",
                     "nfs://","ftp://","dav://","news:","telnet://","imap:","rtsp://","urn:",
                     "pop:","sip:","sips:","tftp:","btspp://","btl2cap://","btgoep://","tcpobex://",
                     "irdaobex://","file://","urn:epc:id:","urn:epc:tag:","urn:epc:pat:","urn:epc:raw:",
                     "urn:epc:","urn:nfc:"]
            plen = msg[0]
            pfx  = msg[2]
            uri_tail = msg[3:3+plen-1].decode(errors="ignore")
            prefix = prefs[pfx] if pfx < len(prefs) else ""
            out.append(f"NDEF URI: {prefix}{uri_tail}")
            return out

        i = 0
        while i < len(msg):
            if i >= len(msg): break
            hdr = msg[i]; i += 1
            if i >= len(msg): break
            tlen = msg[i]; i += 1
            sr = bool(hdr & 0x10)
            if sr:
                if i >= len(msg): break
                plen = msg[i]; i += 1
            else:
                if i + 1 >= len(msg): break
                plen = (msg[i] << 8) | msg[i+1]; i += 2
            il = bool(hdr & 0x08)
            ilen = 0
            if il:
                ilen = msg[i]
                i += 1
            t = msg[i:i+tlen]; i += tlen
            i += ilen
            payload = msg[i:i+plen]; i += plen

            tstr = t.decode(errors="ignore")
            if tstr == "U" and payload:
                prefs=["","http://www.","https://www.","http://","https://","tel:","mailto:",
                       "ftp://anonymous:anonymous@","ftp://ftp.","ftps://","sftp://","smb://",
                       "nfs://","ftp://","dav://","news:","telnet://","imap:","rtsp://","urn:",
                       "pop:","sip:","sips:","tftp:","btspp://","btl2cap://","btgoep://","tcpobex://",
                       "irdaobex://","file://","urn:epc:id:","urn:epc:tag:","urn:epc:pat:","urn:epc:raw:",
                       "urn:epc:","urn:nfc:"]
                prefix = prefs[payload[0]] if payload[0] < len(prefs) else ""
                out.append(f"NDEF URI: {prefix}{payload[1:].decode(errors='ignore')}")
            elif tstr == "T" and payload:
                st = payload[0]; lang_len = st & 0x3F
                lang = payload[1:1+lang_len].decode(errors="ignore")
                text = payload[1+lang_len:].decode(errors="ignore")
                out.append(f'NDEF Text[{lang}]: "{text}"')
            else:
                out.append(f"NDEF {tstr or t.hex()}: {payload.hex()}")

            if hdr & 0x40:  # ME (end)
                break
        return out

    # ---------- CC / capacity ----------

    def _read_cc(self) -> Tuple[Tuple[int,int,int,int], int]:
        """
        Read pages 0..3 (16 bytes). CC is page 3: [E1, v, cap/8, write].
        Returns (CC tuple, capacity bytes) where capacity is None if not NDEF.
        """
        blk0 = self.t.read16(0)
        if len(blk0) != 16:
            raise NDEFWriterError("Failed to read CC (len != 16)")
        cc0, cc1, cc2, cc3 = blk0[12:16]
        capacity = cc2 * 8 if cc0 == 0xE1 else None
        return (cc0, cc1, cc2, cc3), capacity

    @staticmethod
    def _first_user_page() -> int:
        return 4

    @staticmethod
    def _last_user_page_from_capacity(capacity_bytes: int) -> int:
        # user bytes / 4 = pages; start at page 4
        return 4 + (capacity_bytes // 4) - 1

    # ---------- Public API ----------

    def write_url(self, url: str) -> List[str]:
        cc, cap = self._read_cc()
        if cc[0] != 0xE1:
            raise TagNotSupported("Tag is not NDEF-enabled (CC0 != 0xE1).")
        if cap not in (496, 504, 872, 888):
            raise CapacityError(f"Capacity {cap}B not NTAG215/216 (got {cap}).")

        # Build a complete, contiguous TLV
        ndef = self._ndef_uri_bytes(url)
        use_ext = len(ndef) >= 0xFF
        if use_ext:
            tlv = bytearray(b"\x03\xff\x00\x00")
            tlv[2] = (len(ndef) >> 8) & 0xFF
            tlv[3] = (len(ndef) >> 0) & 0xFF
        else:
            tlv = bytearray([0x03, len(ndef)])
        tlv += ndef
        tlv += b"\xFE"  # terminator

        # Pad only AFTER the terminator so we can write full pages
        while len(tlv) % 4:
            tlv += b"\x00"

        first = 4
        last = self._last_user_page_from_capacity(cap)

        # Write sequentially, 4 bytes per page
        for off in range(0, len(tlv), 4):
            page = first + (off // 4)
            if page > last:
                raise NDEFWriterError("Out of user pages while writing TLV.")
            self.t.write4(page, tlv[off:off+4])

        # Verify
        recs = self.verify()
        if not recs:
            raise VerificationError("Wrote data but could not parse any NDEF records.")
        return recs

    def verify(self) -> List[str]:
        """Read beginning of user area, parse first NDEF TLV, return decoded records."""
        cc, cap = self._read_cc()
        if cc[0] != 0xE1:
            return []
        first = self._first_user_page()
        last  = self._last_user_page_from_capacity(cap)

        # Read a reasonable window (first ~64 pages worth of 16B reads)
        buf = bytearray()
        pages_to_scan = min(4 + (256 // 4), (last - first + 1))  # ~64 pages window
        p = first
        while pages_to_scan > 0 and p <= last:
            buf += self.t.read16(p)
            pages_to_scan -= 4
            p += 4

        # Parse TLV
        i = 0; ndef = b""
        while i < len(buf):
            t = buf[i]; i += 1
            if t == 0x00:  # NULL
                continue
            if t == 0xFE:  # Terminator
                break
            if i >= len(buf): break
            L = buf[i]; i += 1
            if L == 0xFF:
                if i + 2 > len(buf): break
                L = (buf[i] << 8) | buf[i+1]; i += 2
            if i + L > len(buf): break
            V = bytes(buf[i:i+L]); i += L
            if t == 0x03:
                ndef = V; break

        return self._parse_ndef_records(ndef) if ndef else []

    def get_uid_hex(self) -> Optional[str]:
        uid = self.t.get_uid()
        if uid is None:
            return None
        return uid.hex().upper()
