#!/usr/bin/env python3
import base64, logging, os, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx
import itchat
from itchat.content import TEXT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PORT     = int(os.environ.get("PORT", "10000"))
API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
API_BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
MODEL    = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "你是一个智能、友好的助手，回答简洁准确。")

qr_b64    = ""
qr_status = "waiting"   # "waiting" | "scanned" | "logged_in"
sessions  = {}
memories  = {}


def qr_callback(uuid, status, qrcode):
    global qr_b64, qr_status
    if status == "0":
        if qrcode:
            qr_b64 = base64.b64encode(qrcode).decode()
        qr_status = "waiting"
        log.info("QR 码已就绪，请访问 /qr 扫码登录")
    elif status == "201":
        qr_status = "scanned"
        log.info("已扫码，等待确认...")
    elif status == "200":
        qr_status = "logged_in"
        qr_b64 = ""
        log.info("微信登录成功！")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/qr":
            if qr_status == "logged_in":
                body = "<html><body><h2>Already logged in!</h2></body></html>".encode("utf-8")
                self._send(200, "text/html; charset=utf-8", body)
            elif qr_status == "scanned":
                body = (
                    "<html><head><meta http-equiv='refresh' content='3'></head>"
                    "<body><h2>已扫码，请在微信点击确认</h2></body></html>"
                ).encode("utf-8")
                self._send(200, "text/html; charset=utf-8", body)
            elif qr_b64:
                img = (
                    "<img src='data:image/jpeg;base64,"
                    + qr_b64
                    + "' style='border:4px solid #07C160;border-radius:8px;width:280px;'>"
                )
                body = (
                    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                    "<meta http-equiv='refresh' content='5'>"
                    "<title>微信扫码登录</title></head>"
                    "<body style='text-align:center;padding:40px'>"
                    "<h2>📱 微信扫码登录</h2>"
                    + img
                    + "<p>用微信扫一扫扫描上方二维码</p></body></html>"
                ).encode("utf-8")
                self._send(200, "text/html; charset=utf-8", body)
            else:
                body = "QR 准备中，请刷新...".encode("utf-8")
                self._send(200, "text/html; charset=utf-8", body)
        else:
            self._send(200, "text/plain", b"WeChat Claude Bridge - Running")

    def _send(self, code, ct, body):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, *_):
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info(f"Health server on port {PORT}")


def call_claude(sender: str, text: str) -> str:
    history = sessions.setdefault(sender, [])
    history.append({"role": "user", "content": text})
    # keep last 20 turns
    if len(history) > 40:
        history[:] = history[-40:]
    try:
        resp = httpx.post(
            f"{API_BASE}/v1/messages",
            headers={
                "x-api-key": API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 1024,
                "system": SYSTEM_PROMPT,
                "messages": history,
            },
            timeout=30,
        )
        resp.raise_for_status()
        reply = resp.json()["content"][0]["text"]
        history.append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return f"（服务暂时不可用，请稍后重试）"


@itchat.msg_register(TEXT)
def handle_text(msg):
    sender = msg.get("FromUserName", "unknown")
    if sender.startswith("@@"):   # ignore group messages
        return
    text = msg.get("Text", "").strip()
    if not text:
        return
    log.info(f"收到消息 from {sender[:8]}...: {text[:50]}")
    reply = call_claude(sender, text)
    itchat.send(reply, toUserName=sender)
    log.info(f"已回复 {sender[:8]}...: {reply[:50]}")


def main():
    start_health_server()
    log.info("启动 itchat 登录流程...")
    itchat.auto_login(
        hotReload=True,
        enableCmdQR=2,
        qrCallback=qr_callback,
        statusStorageDir="itchat.pkl",
    )
    log.info("itchat 已登录，开始监听消息...")
    itchat.run(debug=False)


if __name__ == "__main__":
    main()
