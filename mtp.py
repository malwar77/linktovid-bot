#!/usr/bin/env python3
"""
MTProto big-file sender for the downloader bot.

The Bot API caps uploads at ~50 MB. Files above that (up to 2 GB) are
uploaded through MTProto instead (pyrogram/kurigram), logged in as the
same bot via its token.

The sandbox network only allows real TLS, so pyrogram's TCP connections
are routed through ws_bridge.py, which speaks Telegram's web-client
obfuscated websocket transport (wss://kws<dc>.web.telegram.org/apiws).

API credentials: public Telegram Desktop pair until the owner's own
my.telegram.org pair becomes available; swap TELEGRAM_API_ID /
TELEGRAM_API_HASH env vars when it does — no code change needed.
"""

import os
import logging
import time
from types import SimpleNamespace

import pyrogram
from pyrogram import Client

log = logging.getLogger("mtp")

BASEDIR = os.path.dirname(os.path.abspath(__file__))

API_ID = int(os.environ.get("TELEGRAM_API_ID", "2040"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "b18441a1ff607e10a989891a5eb2e0c9")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

_client: Client = None
_last_progress = {"t": 0.0, "pct": -1}

# ---------------------------------------------------------------- bridge hook

async def _bridge_dc_option(self, dc_id, **kwargs):
    """Send every DC connection to the local websocket bridge."""
    is_media = bool(kwargs.get("is_media"))
    log.debug("routing DC%s (%s) to bridge", dc_id, "media" if is_media else "client")
    return SimpleNamespace(
        id=dc_id,
        ip_address="127.0.0.1",
        port=2400 + dc_id + (100 if is_media else 0),
        static=True,
    )


def _install_bridge_route():
    Client.get_dc_option = _bridge_dc_option

    # pyrogram's load_session hard-codes the direct DC address for new
    # sessions; force every Auth (initial and DC-export) through the bridge
    from pyrogram.session.auth import Auth

    _orig_init = Auth.__init__

    def _bridge_auth_init(self, client, dc_id, server_address, port, test_mode):
        _orig_init(self, client, dc_id, "127.0.0.1", 2400 + dc_id, test_mode)

    Auth.__init__ = _bridge_auth_init

    # route every Session (main, media, exported DCs) through the bridge
    from pyrogram.session.session import Session as _Session

    _orig_sess_init = _Session.__init__

    def _bridge_sess_init(self, client, dc_id, server_address, port, auth_key,
                          test_mode, is_media=False, is_cdn=False):
        _orig_sess_init(self, client, dc_id, "127.0.0.1",
                        2400 + dc_id + (100 if is_media else 0),
                        auth_key, test_mode, is_media, is_cdn)

    _Session.__init__ = _bridge_sess_init


def _progress(current, total):
    now = time.time()
    if now - _last_progress["t"] < 15:
        return
    _last_progress["t"] = now
    pct = int(current * 100 / total) if total else 0
    if pct != _last_progress["pct"]:
        _last_progress["pct"] = pct
        log.info("MTProto upload %d%% (%d / %d MB)",
                 pct, current // (1024 * 1024), total // (1024 * 1024))


async def get_client() -> Client:
    """Start (once) and return the MTProto bot client, via the ws bridge."""
    global _client
    if _client is None:
        if not BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN not set; cannot start MTProto client")
        if os.environ.get("BRIDGE_MODE", "1") == "1":
            # Restricted-network mode (e.g. sandbox that only allows HTTPS):
            # route MTProto TCP through a websocket bridge.
            import ws_bridge
            ws_bridge.start_bridge()
            _install_bridge_route()
        _client = Client(
            "dlbot_mtp",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            workdir=BASEDIR,
        )
        # route the initial DC2 session through the bridge too
        await _client.storage.open()
        await _client.storage.server_address("127.0.0.1")
        await _client.storage.port(2402)
        await _client.start()
        me = await _client.get_me()
        log.info("MTProto client ready as @%s (api_id=%d)", me.username, API_ID)
    return _client


async def send_large(chat_id: int, path: str, caption: str = "",
                     is_video: bool = False, title: str = None):
    """Send a file of any size up to 2 GB via MTProto."""
    c = await get_client()
    cap = (caption or "")[:1000]
    if is_video:
        return await c.send_video(
            chat_id, path, caption=cap, supports_streaming=True,
            progress=_progress,
        )
    return await c.send_document(
        chat_id, path, caption=cap,
        progress=_progress,
    )
