#!/usr/bin/env python3
"""WeChat-Claude Bridge via ClawBot / iLink protocol"""

import base64
import json
import logging
import os
import random
import re
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
CLAUDE_API_BASE = os.environ.get("API_BASE", "https://claude-proxy.haichen940607.workers.dev/v1")
CLAUDE_API_KEY  = os.environ.get("CLAUDE_API_KEY", "sk-UiZRa8dkAviifENhYP6sLhSp6IEf4kyA4mdaI93m7ctpfGxk")
CLAUDE_MODEL    = os.environ.get("CLAUDE_MODEL", "gpt-5.4-low")
MEMORY_BASE     = "https://claude-proxy.haichen940607.workers.dev"
MEMORY_TOKEN    = os.environ.get("MEMORY_TOKEN", "mem_hguo94")

# Image generation keywords (Chinese + English)
IMAGE_KEYWORDS = [
    "生成图", "画一张", "画一幅", "帮我画", "生成一张", "生成一幅",
    "画个", "画出", "生成图片", "生成图像", "create image", "generate image",
    "draw ", "画 ", "画:",
]

# Whisper STT API
WHISPER_API = f"{CLAUDE_API_BASE}/audio/transcriptions"
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


# ── getupdates short-poll loop ───────────────────────────────────────────────
def updates_loop():
    """Short-poll getupdates every 8s — avoids Render free-tier connection kills."""
    log.info("getupdates loop started")
    while True:
        with state_lock:
            if state["status"] != "confirmed":
                return
            buf  = state["updates_buf"]
            burl = state["baseurl"] or ILINK_BASE
        try:
            r = httpx.post(
                f"{burl}/ilink/bot/getupdates",
                headers=bot_headers(),
                json={
                    "get_updates_buf": buf,
                    "base_info": {"channel_version": "1.0.0"},
                },
                timeout=12,
            )
            r.raise_for_status()
            data = r.json()

            # Session expired → reload saved token or re-QR
            if data.get("ret") in (-14, "-14") or data.get("errcode") in (-14, "-14"):
                log.warning("Session expired (-14), reloading saved token...")
                with state_lock:
                    state["bot_token"]   = SAVED_BOT_TOKEN
                    state["updates_buf"] = ""
                if not SAVED_BOT_TOKEN:
                    fetch_qr()
                    threading.Thread(target=poll_qr_status, daemon=True).start()
                    return
                continue

            new_buf = data.get("get_updates_buf", "")
            with state_lock:
                state["updates_buf"] = new_buf if new_buf else buf

            msgs = data.get("msgs") or []
            for msg in msgs:
                threading.Thread(target=handle_message, args=(msg,), daemon=True).start()

        except httpx.TimeoutException:
            pass
        except Exception as e:
            log.error("getupdates error: %s", e)
            time.sleep(3)
        time.sleep(2)


def self_ping_loop():
    """Ping our own /health every 10 min to keep Render free-tier awake."""
    import socket
    host = socket.gethostname()
    own_url = f"http://0.0.0.0:{PORT}/health"
    while True:
        time.sleep(600)
        try:
            httpx.get(own_url, timeout=5)
        except Exception:
            pass


# ── Image generation & hosting ────────────────────────────────────────────────
def upload_to_catbox(img_bytes: bytes) -> str:
    """Upload image bytes to catbox.moe (free, no auth). Returns public URL."""
    try:
        r = httpx.post(
            "https://catbox.moe/user/api.php",
            data={"reqtype": "fileupload"},
            files={"fileToUpload": ("image.png", img_bytes, "image/png")},
            timeout=30,
        )
        if r.status_code == 200 and r.text.startswith("https://"):
            return r.text.strip()
    except Exception as e:
        log.warning("catbox upload failed: %s", e)
    return ""


def upload_to_cf_worker(b64: str) -> str:
    """Upload base64 image to CF Worker /upload-image endpoint. Returns URL or empty."""
    try:
        up = httpx.post(
            f"{MEMORY_BASE}/upload-image",
            json={"token": MEMORY_TOKEN, "data": b64, "ext": "png"},
            timeout=15,
        )
        if up.status_code == 200:
            return up.json().get("url", "")
    except Exception as e:
        log.warning("cf worker upload failed: %s", e)
    return ""


def generate_and_upload_image(prompt: str) -> str:
    """Generate image via gpt-image-2, upload to image host, return public URL."""
    try:
        log.info("Generating image for prompt: %.80s", prompt)
        r = httpx.post(
            f"{CLAUDE_API_BASE}/images/generations",
            headers={"Authorization": f"Bearer {CLAUDE_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-image-2", "prompt": prompt, "n": 1,
                  "size": "1024x1024", "quality": "medium", "response_format": "b64_json"},
            timeout=120,
        )
        r.raise_for_status()
        b64 = r.json()["data"][0]["b64_json"]

        # Try CF Worker first (works if updated worker is deployed)
        url = upload_to_cf_worker(b64)
        if url:
            log.info("Image on CF Worker: %s", url)
            return url

        # Fallback: catbox.moe
        import base64 as _b64
        url = upload_to_catbox(_b64.b64decode(b64))
        if url:
            log.info("Image on catbox: %s", url)
            return url

        log.error("All image upload methods failed")
        return ""
    except Exception as e:
        log.error("generate_and_upload_image error: %s", e)
        return ""


def transcribe_voice(audio_url: str) -> str:
    """Download voice file and transcribe via Whisper."""
    try:
        # Download audio
        audio_resp = httpx.get(audio_url, timeout=30, follow_redirects=True)
        audio_resp.raise_for_status()
        audio_bytes = audio_resp.content
        # Call Whisper API (multipart form)
        files = {"file": ("audio.mp3", audio_bytes, "audio/mpeg")}
        data  = {"model": "whisper-1"}
        r = httpx.post(
            WHISPER_API,
            headers={"Authorization": f"Bearer {CLAUDE_API_KEY}"},
            files=files,
            data=data,
            timeout=60,
        )
        r.raise_for_status()
        text = r.json().get("text", "")
        log.info("Transcribed: %.80s", text)
        return text
    except Exception as e:
        log.error("transcribe_voice error: %s", e)
        return ""


def ask_claude_vision(image_url: str, user_text: str = "", system_prompt: str = "") -> str:
    """Call GPT with an image URL (vision)."""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    content = []
    if user_text:
        content.append({"type": "text", "text": user_text or "请描述这张图片"})
    content.append({"type": "image_url", "image_url": {"url": image_url}})
    messages.append({"role": "user", "content": content})
    try:
        r = httpx.post(
            f"{CLAUDE_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {CLAUDE_API_KEY}", "Content-Type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 1024, "messages": messages},
            timeout=60,
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"]
        return strip_think_tags(raw)
    except Exception as e:
        log.error("ask_claude_vision error: %s", e)
        return "[图片理解失败，请稍后重试]"


def extract_items(msg: dict):
    """Return list of (type, sub_item_dict) from item_list."""
    items = []
    for item in msg.get("item_list") or []:
        t = item.get("type")
        items.append((t, item))
    return items


# ── Message handler ───────────────────────────────────────────────────────────
def handle_message(msg: dict):
    """Process one incoming iLink message and reply (text / image / voice)."""
    log.info("incoming msg: %s", msg)
    try:
        from_user = (
            msg.get("from_user_id") or msg.get("sender_id") or msg.get("from_id") or ""
        )
        ctx_token = (
            msg.get("context_token") or msg.get("contextToken") or ""
        )
        items = extract_items(msg)
        log.info("msg items types: %s", [t for t, _ in items])

        # ── Voice message (type 34 in WeChat, or check for voice_item) ────────
        voice_url = None
        for t, item in items:
            vi = item.get("voice_item") or item.get("voiceItem") or {}
            if vi.get("url") or vi.get("voice_url"):
                voice_url = vi.get("url") or vi.get("voice_url")
                break
            # fallback: some protocols use type=34 or "audio"
            if t in (34, "34", "voice", "audio"):
                voice_url = (item.get("url") or item.get("voice_url") or
                             (item.get("media_item") or {}).get("url") or "")
                break

        if voice_url:
            log.info("Voice message, url: %s", voice_url)
            text = transcribe_voice(voice_url)
            if not text:
                send_reply(from_user, ctx_token, "🎙️ 语音识别失败，请重试或发送文字")
                return
            # Reply with transcription + AI answer
            memory_ctx = fetch_memory()
            prompt = f"[用户发送了语音，转文字如下]\n{text}"
            reply = ask_claude(prompt, system_prompt=memory_ctx)
            full_reply = f"🎙️ 你说的是：{text}\n\n{reply}"
            threading.Thread(target=append_history, args=(text, reply), daemon=True).start()
            send_reply(from_user, ctx_token, full_reply)
            return

        # ── Image message (type 3 or image_item present) ──────────────────────
        image_url = None
        image_text = ""
        for t, item in items:
            ii = item.get("image_item") or item.get("imageItem") or {}
            candidate = (ii.get("url") or ii.get("image_url") or
                         item.get("url") or "")
            if candidate and candidate.startswith("http"):
                image_url = candidate
                break
            if t in (3, "3", "image"):
                image_url = (item.get("url") or
                             (item.get("image_item") or {}).get("url") or "")
                if image_url:
                    break
        # Also grab any accompanying text
        for t, item in items:
            if t == 1:
                image_text = (item.get("text_item") or {}).get("text", "")
                break

        if image_url:
            log.info("Image message, url: %s", image_url)
            memory_ctx = fetch_memory()
            prompt_text = image_text or "请描述这张图片的内容"
            reply = ask_claude_vision(image_url, user_text=prompt_text, system_prompt=memory_ctx)
            threading.Thread(target=append_history,
                             args=(f"[图片] {prompt_text}", reply), daemon=True).start()
            send_reply(from_user, ctx_token, reply)
            return

        # ── Text message ──────────────────────────────────────────────────────
        text = ""
        for t, item in items:
            if t == 1:
                text = (item.get("text_item") or {}).get("text", "")
                break

        if not text:
            log.info("Unknown message type, skipping: %s", [t for t, _ in items])
            return

        log.info("Text from %s: %s", from_user, text)

        # Detect image generation request
        text_lower = text.lower()
        if any(kw in text_lower for kw in IMAGE_KEYWORDS):
            # Strip the trigger phrase to get the actual prompt
            prompt = text
            for kw in IMAGE_KEYWORDS:
                prompt = prompt.replace(kw, "").strip()
            if not prompt:
                prompt = text  # fallback: use full text as prompt
            send_reply(from_user, ctx_token, "🎨 正在生成图片，请稍候...")
            img_url = generate_and_upload_image(prompt)
            if img_url:
                # Send image via WeChat
                send_image(from_user, ctx_token, img_url)
            else:
                send_reply(from_user, ctx_token, "😔 图片生成失败，请稍后重试")
            threading.Thread(target=append_history,
                             args=(text, f"[已生成图片] {img_url}"), daemon=True).start()
            return

        # Normal text reply
        memory_ctx = fetch_memory()
        reply = ask_claude(text, system_prompt=memory_ctx)
        log.info("Claude reply: %.80s", reply)
        threading.Thread(target=append_history, args=(text, reply), daemon=True).start()
        send_reply(from_user, ctx_token, reply)

    except Exception as e:
        log.error("handle_message error: %s", e)


def fetch_memory() -> str:
    """从 CF KV 取 Mac Claude 记忆 + 最近微信对话历史，拼成 system prompt。"""
    try:
        mac_mem = ""
        wechat_hist = ""
        r1 = httpx.get(f"{MEMORY_BASE}/memory?key=claude_memory", timeout=5)
        if r1.status_code == 200:
            mac_mem = r1.json().get("value") or ""
        r2 = httpx.get(f"{MEMORY_BASE}/memory?key=wechat_history", timeout=5)
        if r2.status_code == 200:
            hist_raw = r2.json().get("value") or "[]"
            hist = json.loads(hist_raw)
            # 只取最近 10 条
            recent = hist[-10:]
            if recent:
                lines = [f"[{h['ts'][:16]}] 用户: {h['user']}\nAI: {h['ai']}" for h in recent]
                wechat_hist = "\n\n".join(lines)
        parts = []
        if mac_mem:
            parts.append(f"【用户背景（来自 Mac Claude 记忆）】\n{mac_mem}")
        if wechat_hist:
            parts.append(f"【近期微信对话记录】\n{wechat_hist}")
        return "\n\n".join(parts)
    except Exception as e:
        log.warning("fetch_memory failed: %s", e)
        return ""


def append_history(user_text: str, ai_reply: str):
    """把这轮对话异步写入 CF KV。"""
    try:
        httpx.post(
            f"{MEMORY_BASE}/memory/append",
            json={"token": MEMORY_TOKEN, "user": user_text, "ai": ai_reply},
            timeout=5,
        )
    except Exception as e:
        log.warning("append_history failed: %s", e)


def strip_think_tags(text: str) -> str:
    """Remove <think>...</think> reasoning blocks that some models output."""
    # Remove <think>...</think> blocks (including multiline)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()


def ask_claude(user_text: str, system_prompt: str = "") -> str:
    last_err = None
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_text})
    for attempt in range(4):          # up to 4 tries
        if attempt:
            time.sleep(3 * attempt)   # 3s, 6s, 9s back-off
        try:
            r = httpx.post(
                f"{CLAUDE_API_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {CLAUDE_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":      CLAUDE_MODEL,
                    "max_tokens": 1024,
                    "messages":   messages,
                },
                timeout=60,
            )
            if r.status_code == 503:
                log.warning("API 503, retry %d/4", attempt + 1)
                last_err = f"503 on attempt {attempt+1}"
                continue
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"]
            return strip_think_tags(raw)
        except Exception as e:
            log.error("ask_gpt attempt %d error: %s", attempt + 1, e)
            last_err = e
    return "[AI 暂时不可用，请稍后再试]"


def _bot_url(path: str) -> str:
    """Build full iLink bot API URL regardless of whether baseurl includes /ilink/bot."""
    with state_lock:
        burl = (state["baseurl"] or ILINK_BASE).rstrip("/")
    if "/ilink/bot" not in burl:
        burl = burl + "/ilink/bot"
    return f"{burl}/{path.lstrip('/')}"


def send_image(to_user: str, context_token: str, image_url: str):
    """Send an image message via iLink sendmessage."""
    try:
        payload = {
            "msg": {
                "to_user_id":    to_user,
                "client_id":     str(uuid.uuid4()),
                "message_type":  2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [
                    {
                        "type": 3,
                        "image_item": {"url": image_url},
                    }
                ],
            },
            "base_info": {"channel_version": "1.0.0"},
        }
        r = httpx.post(
            _bot_url("sendmessage"),
            headers=bot_headers(),
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        log.info("send_image response: %s", r.json())
    except Exception as e:
        log.error("send_image error: %s", e)
        # Fallback: send URL as text
        send_reply(to_user, context_token, f"🖼️ 图片链接：{image_url}")


def send_reply(to_user: str, context_token: str, text: str):
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
            _bot_url("sendmessage"),
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

        elif path == "/test-api":
            # Test Claude API reachability from this server
            import time as _t; t0 = _t.time()
            result = ask_claude("reply with just: ok")
            elapsed = round(_t.time() - t0, 2)
            body = json.dumps({"result": result, "elapsed_s": elapsed,
                               "model": CLAUDE_MODEL, "base": CLAUDE_API_BASE},
                              ensure_ascii=False)
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
        threading.Thread(target=updates_loop,   daemon=True).start()
        threading.Thread(target=self_ping_loop, daemon=True).start()
    else:
        # No token: show QR for auth
        threading.Thread(target=fetch_qr,       daemon=True).start()
        threading.Thread(target=poll_qr_status, daemon=True).start()

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info("HTTP server on port %d", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
