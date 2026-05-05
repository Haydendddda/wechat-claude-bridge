#!/usr/bin/env python3
"""WeChat-Claude Bridge via ClawBot / iLink protocol"""

import base64
import json
import logging
import os
import random
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bridge")

# ── Config ────────────────────────────────────────────────────────────────────
ILINK_BASE      = "https://ilinkai.weixin.qq.com/ilink/bot"
CLAUDE_API_BASE = os.environ.get("API_BASE", "https://api.mttieeo.com/v1")
CLAUDE_API_KEY  = os.environ.get("CLAUDE_API_KEY", "")
CLAUDE_MODEL    = os.environ.get("CLAUDE_MODEL", "claude-opus-4-5")
PORT = int(os.environ.get("PORT", 10000))

# Pre-authorized credentials (set via env var BOT_TOKEN to override)
_DEFAULT_BOT_TOKEN    = "e06aeeccb5de@im.bot:060000986326dcc5316c494ecaa142f1360bd7"
_DEFAULT_BOT_ID       = "e06aeeccb5de@im.bot"
_DEFAULT_USER_ID      = "o9cq80_q9fDpWkVGgVUqwJv8UKFo@im.wechat"
_DEFAULT_BASEURL      = "https://ilinkai.weixin.qq.com"
SAVED_BOT_TOKEN = os.environ.get("BOT_TOKEN", _DEFAULT_BOT_TOKEN)
SAVED_BOT_ID    = os.environ.get("ILINK_BOT_ID", _DEFAULT_BOT_ID)
SAVED_USER_ID   = os.environ.get("ILINK_USER_ID", _DEFAULT_USER_ID)
SAVED_BASEURL   = os.environ.get("ILINK_BASEURL", _DEFAULT_BASEURL)

# ── Runtime state ─────────────────────────────────────────────────────────────
state = {
    "qrcode_token":   None,   # opaque polling token from get_bot_qrcode
    "qrcode_img_url": None,   # URL to render as QR image
    "bot_token":      None,   # filled after user confirms scan
    "ilink_bot_id":   None,
    "ilink_user_id":  None,
    "baseurl":        None,
    "updates_buf":    "",     # getupdates cursor (empty string for first call)
    "status":         "init", # init | waiting | confirmed | expired
}
state_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────
def wx_uin_header() -> str:
    """X-WECHAT-UIN = base64(str(random uint32))"""
    return base64.b64encode(str(random.getrandbits(32)).encode()).decode()


def bot_headers() -> dict:
    with state_lock:
        token = state["bot_token"]
    return {
        "Content-Type":      "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization":     f"Bearer {token}",
        "X-WECHAT-UIN":      wx_uin_header(),
    }


# ── QR / login flow ───────────────────────────────────────────────────────────
def fetch_qr():
    """Call get_bot_qrcode and store tokens in state."""
    try:
        r = httpx.get(
            f"{ILINK_BASE}/get_bot_qrcode",
            params={"bot_type": "3"},
            headers={"SKRouteTag": "1001"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        log.info("get_bot_qrcode response: %s", data)
        with state_lock:
            state["qrcode_token"]   = data.get("qrcode") or data.get("qrcode_id")
            state["qrcode_img_url"] = (
                data.get("qrcode_img_content")
                or data.get("qrcode_url")
                or data.get("url")
            )
            state["status"] = "waiting"
        log.info("QR token=%s  img_url=%s",
                 state["qrcode_token"], state["qrcode_img_url"])
    except Exception as e:
        log.error("fetch_qr failed: %s", e)


def poll_qr_status():
    """Background thread: poll get_qrcode_status until confirmed or expired."""
    while True:
        with state_lock:
            tok = state["qrcode_token"]
            st  = state["status"]
        if st == "confirmed":
            return
        if not tok:
            time.sleep(3)
            continue
        try:
            r = httpx.get(
                f"{ILINK_BASE}/get_qrcode_status",
                params={"qrcode": tok},
                headers={"iLink-App-ClientVersion": "1"},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            log.info("qrcode_status: %s", data)
            status = data.get("status", "")

            if status == "confirmed":
                with state_lock:
                    state["bot_token"]     = data.get("bot_token")
                    state["ilink_bot_id"]  = data.get("ilink_bot_id")
                    state["ilink_user_id"] = data.get("ilink_user_id")
                    state["baseurl"]       = data.get("baseurl", ILINK_BASE)
                    state["status"]        = "confirmed"
                log.info("Login confirmed! bot_token=%s", state["bot_token"])
                threading.Thread(target=updates_loop, daemon=True).start()
                return
            elif status == "expired":
                log.warning("QR expired, fetching new one...")
                with state_lock:
                    state["status"]         = "init"
                    state["qrcode_token"]   = None
                    state["qrcode_img_url"] = None
                fetch_qr()
        except Exception as e:
            log.error("poll_qr_status error: %s", e)
        time.sleep(3)


# ── getupdates long-poll loop ─────────────────────────────────────────────────
def updates_loop():
    """Long-poll getupdates; dispatch each message to handle_message."""
    log.info("getupdates loop started")
    while True:
        with state_lock:
            if state["status"] != "confirmed":
                return
            buf  = state["updates_buf"]
            burl = state["baseurl"] or ILINK_BASE
        try:
            r = httpx.post(
                f"{burl}/getupdates",
                headers=bot_headers(),
                json={
                    "get_updates_buf": buf,
                    "base_info": {"channel_version": "1.0.0"},
                },
                timeout=40,
            )
            r.raise_for_status()
            data = r.json()

            # Session expired → re-login
            if data.get("ret") in (-14, "-14") or data.get("errcode") in (-14, "-14"):
                log.warning("Session expired (-14), re-logging in...")
                with state_lock:
                    state["status"]      = "init"
                    state["bot_token"]   = None
                    state["updates_buf"] = ""
                fetch_qr()
                threading.Thread(target=poll_qr_status, daemon=True).start()
                return

            new_buf = data.get("get_updates_buf", "")
            with state_lock:
                state["updates_buf"] = new_buf if new_buf else buf

            msgs = data.get("msgs") or []
            for msg in msgs:
                threading.Thread(target=handle_message, args=(msg,), daemon=True).start()

        except httpx.TimeoutException:
            pass  # normal for 35s long-poll
        except Exception as e:
            log.error("getupdates error: %s", e)
            time.sleep(5)


# ── Message handler ───────────────────────────────────────────────────────────
def handle_message(msg: dict):
    """Process one incoming iLink message and reply."""
    log.info("incoming msg: %s", msg)
    try:
        # Extract text from item_list
        text = ""
        for item in msg.get("item_list") or []:
            if item.get("type") == 1:
                text = (item.get("text_item") or {}).get("text", "")
                break
        if not text:
            log.info("Skipping non-text message")
            return

        from_user = (
            msg.get("from_user_id")
            or msg.get("sender_id")
            or msg.get("from_id")
            or ""
        )
        ctx_token = (
            msg.get("context_token")
            or msg.get("contextToken")
            or ""
        )
        log.info("Text from %s: %s", from_user, text)

        reply = ask_claude(text)
        log.info("Claude reply: %.80s", reply)

        send_reply(from_user, ctx_token, reply)
    except Exception as e:
        log.error("handle_message error: %s", e)


def ask_claude(user_text: str) -> str:
    try:
        r = httpx.post(
            f"{CLAUDE_API_BASE}/messages",
            headers={
                "x-api-key":         CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type":      "application/json",
            },
            json={
                "model":     CLAUDE_MODEL,
                "max_tokens": 1024,
                "messages":  [{"role": "user", "content": user_text}],
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"]
    except Exception as e:
        log.error("ask_claude error: %s", e)
        return f"[Claude error: {e}]"


def send_reply(to_user: str, context_token: str, text: str):
    with state_lock:
        burl = state["baseurl"] or ILINK_BASE
    try:
        payload = {
            "msg": {
                "to_user_id":    to_user,
                "client_id":     str(uuid.uuid4()),
                "message_type":  2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [
                    {"type": 1, "text_item": {"text": text}}
                ],
            },
            "base_info": {"channel_version": "1.0.0"},
        }
        r = httpx.post(
            f"{burl}/sendmessage",
            headers=bot_headers(),
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        log.info("sendmessage response: %s", r.json())
    except Exception as e:
        log.error("send_reply error: %s", e)


# ── HTTP server ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress noisy access log

    def _html(self, code, body):
        enc = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(enc)))
        self.end_headers()
        self.wfile.write(enc)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/health"):
            with state_lock:
                st  = state["status"]
                bot = state["ilink_bot_id"]
            if st == "confirmed":
                body = f"<h2>Bot online</h2><p>bot_id: {bot}</p>"
            elif st == "waiting":
                body = '<h2>Waiting for scan</h2><p><a href="/qr">Open QR page</a></p>'
            else:
                body = '<h2>Starting...</h2><p><a href="/qr">Get QR</a></p>'
            self._html(200, body)

        elif path == "/qr":
            with state_lock:
                img_url = state["qrcode_img_url"]
                st      = state["status"]

            if st == "confirmed":
                self._html(200,
                    "<h2>Already logged in!</h2>"
                    "<p>Bot is running. You can close this page.</p>")
                return

            # Trigger fresh QR if needed
            if not img_url:
                fetch_qr()
                time.sleep(1.5)
                with state_lock:
                    img_url = state["qrcode_img_url"]

            if img_url:
                import urllib.parse as _up
                qr_img_src = (
                    "https://api.qrserver.com/v1/create-qr-code/"
                    f"?size=280x280&margin=10&data={_up.quote(img_url, safe='')}"
                )
                html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>WeChat ClawBot Auth</title>
<style>
  body{{font-family:sans-serif;text-align:center;padding:40px;background:#f5f5f5}}
  h2{{color:#07c160}}
  .qr-wrap{{display:inline-block;border:8px solid #07c160;
            border-radius:12px;padding:10px;background:#fff;margin:20px}}
  img{{display:block;width:280px;height:280px}}
  p{{color:#555;font-size:14px}}
  .sub{{color:#999;font-size:12px}}
</style>
</head><body>
<h2>微信扫码授权 ClawBot</h2>
<p>打开微信 &rarr; 扫一扫，扫描下方二维码</p>
<div class="qr-wrap">
  <img src="{qr_img_src}" alt="ClawBot Auth QR">
</div>
<p class="sub">页面每5秒自动刷新 &middot; 扫码并点击「确认登录」后机器人自动上线</p>
</body></html>"""
            else:
                html = "<h2>获取二维码失败，请刷新重试</h2>"
            self._html(200, html)

        elif path == "/status":
            with state_lock:
                snap = dict(state)
                snap["bot_token"] = "***" if snap["bot_token"] else None
            body = json.dumps(snap, ensure_ascii=False, indent=2)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())

        else:
            self._html(404, "<h2>404</h2>")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    if SAVED_BOT_TOKEN:
        # Pre-authorized: skip QR flow, go straight to message polling
        with state_lock:
            state["bot_token"]     = SAVED_BOT_TOKEN
            state["ilink_bot_id"]  = SAVED_BOT_ID
            state["ilink_user_id"] = SAVED_USER_ID
            state["baseurl"]       = SAVED_BASEURL
            state["status"]        = "confirmed"
        log.info("Using saved bot_token, starting getupdates loop directly")
        threading.Thread(target=updates_loop, daemon=True).start()
    else:
        # No token: show QR for auth
        threading.Thread(target=fetch_qr,       daemon=True).start()
        threading.Thread(target=poll_qr_status, daemon=True).start()

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info("HTTP server on port %d", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
