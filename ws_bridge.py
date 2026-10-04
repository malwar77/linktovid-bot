#!/usr/bin/env python3
"""
WebSocket MTProto bridge for the sandbox.

The sandbox egress network only permits real TLS (port 443). Raw MTProto
TCP to Telegram's DCs is blocked, but Telegram's own web clients reach MTProto
over a binary WebSocket (wss://kws<dc>.web.telegram.org/apiws, subprotocol
"binary") — which is genuine TLS and passes.

This bridge lets pyrogram connect as usual over TCP to 127.0.0.1 and relays
the byte stream through the web client's obfuscated websocket transport:

  * Client sends first a 64-byte init: 56 random bytes + AES-CTR-encrypted
    [obfuscate tag (efefefef for abridged)][4 random bytes].
  * The outgoing keystream uses AES-CTR(key=init[8:40], iv-counter=init[40:56]).
  * The incoming keystream uses AES-CTR over the same construction from the
    REVERSED init payload.
  * After the init, the stream carries abridged frames (the leading 0xef
    magic of plain abridged is not sent again; the tag in the init replaces
    it), each ws message carrying whatever bytes of the stream.
  * The server sends no init of its own; its very first message already
    carries obfuscated abridged frames encrypted with the incoming keystream.

This mirrors tweb's Obfuscation + TcpObfuscated + Socket (websocket.ts) and
pyrogram's TCPAbridged on the TCP side; the pyrogram magic byte 0xef is
swallowed here. Byte boundaries are irrelevant (it is a stream cipher), so
no frame parsing is needed anywhere.

Port map:
  2401-2405  -> wss://kws<dc>.web.telegram.org/apiws          (client DCs)
  2501-2505  -> wss://kws<dc>-1.web.telegram.org/apiws       (media DCs)
"""

import os
import struct
import socket
import threading
import logging

import websocket
from Crypto.Cipher import AES
from Crypto.Util import Counter

log = logging.getLogger("wsbridge")

WS_HOST_TPL = "wss://kws{dc}{suffix}.web.telegram.org/apiws"
SUBPROTOCOL = "binary"
BANNED_INTS = {
    0x44414548, 0x54534f50, 0x20544547,
    0x4954504f, 0xeeeeeeee, 0xdddddddd,
}


def make_init():
    """Build the 64-byte init per the web client's Obfuscation.init()."""
    while True:
        init = bytearray(os.urandom(64))
        v1 = struct.unpack("<I", init[0:4])[0]
        v2 = struct.unpack("<I", init[4:8])[0]
        if init[0] != 0xEF and v1 not in BANNED_INTS and v2 != 0:
            break

    rev = bytes(init[::-1])

    enc = AES.new(
        bytes(init[8:40]), AES.MODE_CTR,
        counter=Counter.new(128, initial_value=int.from_bytes(init[40:56], "big")),
    )
    dec = AES.new(
        rev[8:40], AES.MODE_CTR,
        counter=Counter.new(128, initial_value=int.from_bytes(rev[40:56], "big")),
    )

    # abridged obfuscate tag; consuming the keystream on the whole 64 bytes
    # is part of the protocol (the first 4 blocks advance the stream)
    init[56:60] = b"\xef\xef\xef\xef"
    enc_all = enc.encrypt(bytes(init))
    wire_init = bytes(init[0:56]) + enc_all[56:64]
    return wire_init, enc, dec


DEBUG = os.environ.get("BRIDGE_DEBUG")
_dbg_sent = {"n": 0}
_dbg_recv = {"n": 0}


def _dbg_out(tag, data):
    if not DEBUG:
        return
    if tag == "out" and _dbg_sent["n"] < 6:
        _dbg_sent["n"] += 1
        print(f"[BRIDGE out {_dbg_sent['n']}] {data[:60].hex()}", flush=True)
    elif tag == "in" and _dbg_recv["n"] < 6:
        _dbg_recv["n"] += 1
        print(f"[BRIDGE in  {_dbg_recv['n']}] raw={data[:60].hex()}", flush=True)


def bridge_conn(csock: socket.socket, dc: int, media: bool):
    """Handle one pyrogram TCP connection through the websocket transport."""
    suffix = "-1" if media else ""
    url = WS_HOST_TPL.format(dc=dc, suffix=suffix)
    ws = None
    try:
        ws = websocket.create_connection(
            url,
            timeout=30,
            subprotocols=[SUBPROTOCOL],
            enable_multithread=True,
            header={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        )
    except Exception as e:
        log.error("ws connect failed %s: %r", url, e)
        try:
            csock.close()
        except OSError:
            pass
        return

    wire_init, enc, dec = make_init()
    try:
        ws.send_binary(wire_init)

        # swallow pyrogram's abridged magic byte
        first = csock.recv(1)
        _dbg_sent["n"] = 0
        _dbg_recv["n"] = 0
        if first != b"\xef":
            # not abridged; push it back into the stream as-is
            ws.send_binary(enc.encrypt(first))
            _dbg_out("out", first)

        def tcp_to_ws():
            try:
                while True:
                    chunk = csock.recv(65536)
                    if not chunk:
                        break
                    wire = enc.encrypt(chunk)
                    _dbg_out("out", wire)
                    ws.send_binary(wire)
            except OSError:
                pass
            finally:
                try:
                    ws.close()
                except Exception:
                    pass

        def ws_to_tcp():
            try:
                while True:
                    data = ws.recv()
                    if not data:
                        break
                    _dbg_out("in", data)
                    csock.sendall(dec.decrypt(data))
            except Exception:
                pass
            finally:
                try:
                    csock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        t = threading.Thread(target=tcp_to_ws, daemon=True)
        t.start()
        ws_to_tcp()
        t.join(timeout=5)
    except Exception as e:
        log.debug("bridge session ended: %r", e)
    finally:
        try:
            ws.close()
        except Exception:
            pass
        try:
            csock.close()
        except OSError:
            pass


def listener(port: int, dc: int, media: bool):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(16)
    log.info("bridge listening on 127.0.0.1:%d -> %s%s",
             port, WS_HOST_TPL.format(dc=dc, suffix="-1" if media else ""),
             " (media)" if media else "")
    while True:
        csock, _ = srv.accept()
        threading.Thread(
            target=bridge_conn, args=(csock, dc, media), daemon=True
        ).start()


_started = False


def start_bridge():
    """Start all bridge listeners once."""
    global _started
    if _started:
        return
    _started = True
    for dc in range(1, 6):
        threading.Thread(target=listener, args=(2400 + dc, dc, False), daemon=True).start()
        threading.Thread(target=listener, args=(2500 + dc, dc, True), daemon=True).start()
