#!/usr/bin/env python3
"""
WeChat Claude Bridge
- 通过 iLink API 接收微信 ClawBot 消息
- 直接调用 Claude API（OpenAI 兼容格式）回复
- 支持持久记忆、对话历史
- 内置 HTTP 健康检查（供 Render 使用）
"""

import base64
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import qrcode

# ─── 配置 ────────────────────────────────────────────────────────────────────
ILINK_BASE   = "https://ilinkai.weixin.qq.com"
API_KEY      = os.environ.get("ANTHROPIC_API_KEY", "")
API_BASE     = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
MODEL        = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")
CONFIG_DIR   = Path(os.environ.get("CONFIG_DIR", "~/.config/wechat-claude-bridge")).expanduser()
PORT         = int(os.environ.get("PORT", "10000"))
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "你是一个智能、友好的助手，回答简洁准确。")

# ─── 日志 ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── 状态 ────────────────────────────────────────────────────────────────────
state    = {}          # token, cursor 等
sessions = {}          # openid -> [{role, content}, ...]
memories = {}          # openid -> str


# ─── 持久化 ──────────────────────────────────────────────────────────────────
def cfg(name: str) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR / name


def load_state():
    # 1. 从环境变量读取 token（Render 重启后自动恢复）
    b64 = os.environ.get("WECHAT_TOKEN_B64", "")
    if b64:
        try:
            state.update(json.loads(base64.b64decode(b64)))
            log.info("从环境变量恢复登录 token")
            cfg("token.json").write_text(json.dumps(state))
        except Exception as e:
            log.warning(f"环境变量 token 解析失败: {e}")

    # 2. 从文件读取
    tf = cfg("token.json")
    if not state.get("token") and tf.exists():
        try:
            state.update(json.loads(tf.read_text()))
            log.info("从文件恢复登录 token")
        except Exception:
            pass

    # 3. 读取记忆
    mf = cfg("memory.json")
    if mf.exists():
        try:
            memories.update(json.loads(mf.read_text()))
        except Exception:
            pass


def save_token():
    cfg("token.json").write_text(json.dumps(state))
    b64 = base64.b64encode(json.dumps(state).encode()).decode()
    log.info("=" * 60)
    log.info("登录成功！请复制下方 token 保存为 Render 环境变量 WECHAT_TOKEN_B64：")
    log.info(b64)
    log.info("=" * 60)


def save_memory():
    cfg("memory.json").write_text(json.dumps(memories, ensure_ascii=False))


# ─── 健康检查 HTTP 服务 ────────────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"WeChat Claude Bridge - Running"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # 关闭 HTTP 日志


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info(f"健康检查服务器运行在 :{PORT}")


# ─── iLink API ───────────────────────────────────────────────────────────────
def ilink_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if state.get("token"):
        h["Authorization"] = f"Bearer {state['token']}"
    return h


def do_login(client: httpx.Client) -> bool:
    """扫码登录流程"""
    # 获取二维码
    r = client.get(f"{ILINK_BASE}/ilink/bot/get_bot_qrcode", params={"bot_type": "3"})
    d = r.json()
    log.debug(f"QR 响应: {d}")

    def extract(d, *keys):
        for k in keys:
            v = d.get(k) or d.get("data", {}).get(k)
            if v:
                return v
        return ""

    qr_id  = extract(d, "qrcode", "ticket", "code")
    qr_url = extract(d, "qrcode_url", "url", "link")

    if not qr_url and qr_id:
        qr_url = f"https://liteapp.weixin.qq.com/q/{qr_id}?bot_type=3"

    if not qr_url:
        log.error(f"无法获取二维码 URL，响应: {d}")
        return False

    # 显示二维码
    qr = qrcode.QRCode(border=1)
    qr.add_data(qr_url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    log.info(f"请用微信扫描上方二维码（或打开链接）: {qr_url}")
    log.info("等待扫码...")

    # 轮询扫码状态
    for _ in range(120):
        time.sleep(2)
        r = client.get(
            f"{ILINK_BASE}/ilink/bot/get_qrcode_status",
            params={"qrcode": qr_id},
        )
        d = r.json()
        log.debug(f"扫码状态: {d}")

        token = extract(d, "access_token", "token", "auth_token")
        if token:
            state["token"]  = token
            state["cursor"] = ""
            save_token()
            log.info("登录成功！")
            return True

    log.error("扫码超时（2 分钟），请重启程序重试")
    return False


def get_updates(client: httpx.Client):
    """长轮询获取新消息，返回 (messages, need_relogin)"""
    body = {"timeout": 20}
    if state.get("cursor"):
        body["cursor"] = state["cursor"]

    try:
        r = client.post(
            f"{ILINK_BASE}/ilink/bot/getupdates",
            headers=ilink_headers(),
            json=body,
            timeout=30.0,
        )
        d = r.json()
    except Exception as e:
        log.error(f"getupdates 异常: {e}")
        return [], False

    errcode = d.get("errcode", 0)
    if errcode in (-1, 401, 40001, 42001):
        log.warning(f"Token 失效 (errcode={errcode})，需要重新登录")
        return [], True

    msgs = (
        d.get("msg_list")
        or d.get("messages")
        or (d.get("data") or {}).get("msg_list")
        or (d.get("data") or {}).get("messages")
        or []
    )

    new_cursor = d.get("cursor") or (d.get("data") or {}).get("cursor")
    if new_cursor:
        state["cursor"] = new_cursor

    return msgs, False


def send_message(client: httpx.Client, to_user: str, content: str):
    """发送文本消息给用户"""
    try:
        r = client.post(
            f"{ILINK_BASE}/ilink/bot/sendmessage",
            headers=ilink_headers(),
            json={
                "to_user":   to_user,
                "msg_type":  1,
                "text_item": {"content": content},
            },
        )
        return r.json()
    except Exception as e:
        log.error(f"sendmessage 异常: {e}")


def parse_message(raw: dict) -> tuple[str, str]:
    """从原始消息提取发送者 ID 和文本内容"""
    sender = (
        raw.get("from_user")
        or raw.get("open_id")
        or raw.get("sender")
        or raw.get("from_wxid")
        or "unknown"
    )

    text = ""
    ti = raw.get("text_item") or {}
    if isinstance(ti, dict):
        text = ti.get("content") or ti.get("text") or ""
    elif isinstance(ti, str):
        text = ti

    if not text:
        text = raw.get("content") or raw.get("text") or ""

    return sender, text.strip()


# ─── Claude API ───────────────────────────────────────────────────────────────
def handle_command(openid: str, msg: str) -> str | None:
    """处理 / 命令，返回回复字符串；不是命令则返回 None"""
    if msg.startswith("/remember "):
        item = msg[10:].strip()
        memories[openid] = (memories.get(openid, "") + f"\n- {item}").strip()
        save_memory()
        return f"✅ 已记住：{item}"

    if msg == "/memory":
        mem = memories.get(openid, "")
        return f"📝 我的记忆：\n{mem}" if mem else "📝 暂无记忆（用 /remember 添加）"

    if msg == "/clear":
        sessions.pop(openid, None)
        return "✅ 对话历史已清除"

    if msg == "/help":
        return (
            "可用命令：\n"
            "/remember <内容> — 保存长期记忆\n"
            "/memory — 查看所有记忆\n"
            "/clear — 清除当前对话历史\n"
            "/help — 显示此帮助"
        )

    return None


def call_claude(openid: str, user_msg: str) -> str:
    """调用 Claude API 并返回回复"""
    cmd_reply = handle_command(openid, user_msg)
    if cmd_reply is not None:
        return cmd_reply

    if not API_KEY:
        return "⚠️ 未配置 ANTHROPIC_API_KEY，请在 Render 环境变量中设置"

    # 构建系统提示（含记忆）
    system = SYSTEM_PROMPT
    if memories.get(openid):
        system += f"\n\n【关于用户的记忆，每次对话自动加载】\n{memories[openid]}"

    hist = sessions.get(openid, [])
    messages = hist[-20:] + [{"role": "user", "content": user_msg}]

    with httpx.Client(timeout=60.0) as c:
        # 优先尝试 OpenAI 兼容格式（mttieeo.com 使用此格式）
        try:
            oai_msgs = [{"role": "system", "content": system}] + messages
            r = c.post(
                f"{API_BASE}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={"model": MODEL, "messages": oai_msgs, "max_tokens": 2048},
            )
            if r.status_code == 200:
                reply = r.json()["choices"][0]["message"]["content"]
                sessions[openid] = (
                    hist + [
                        {"role": "user",      "content": user_msg},
                        {"role": "assistant", "content": reply},
                    ]
                )[-40:]
                return reply
            log.warning(f"OpenAI 格式错误 {r.status_code}: {r.text[:300]}")
        except Exception as e:
            log.debug(f"OpenAI 格式异常: {e}")

        # 备用：Anthropic 原生格式
        try:
            r = c.post(
                f"{API_BASE}/v1/messages",
                headers={
                    "x-api-key":         API_KEY,
                    "anthropic-version": "2023-06-01",
                    "Content-Type":      "application/json",
                },
                json={
                    "model":      MODEL,
                    "max_tokens": 2048,
                    "system":     system,
                    "messages":   messages,
                },
            )
            if r.status_code == 200:
                reply = r.json()["content"][0]["text"]
                sessions[openid] = (
                    hist + [
                        {"role": "user",      "content": user_msg},
                        {"role": "assistant", "content": reply},
                    ]
                )[-40:]
                return reply
            log.error(f"Anthropic 格式错误 {r.status_code}: {r.text[:300]}")
        except Exception as e:
            log.error(f"Anthropic 格式异常: {e}")

    return "⚠️ API 调用失败，请检查 Render 日志"


# ─── 主循环 ───────────────────────────────────────────────────────────────────
def main():
    load_state()
    start_health_server()

    with httpx.Client(timeout=60.0) as client:
        if not state.get("token"):
            if not do_login(client):
                sys.exit(1)

        log.info("═" * 50)
        log.info("WeChat Claude Bridge 已启动")
        log.info(f"API Base : {API_BASE}")
        log.info(f"Model    : {MODEL}")
        log.info("监听微信消息中... (Ctrl+C 停止)")
        log.info("═" * 50)

        errors = 0
        while True:
            try:
                msgs, need_login = get_updates(client)

                if need_login:
                    state["token"] = ""
                    log.info("重新登录中...")
                    if not do_login(client):
                        time.sleep(60)
                    continue

                for raw in msgs or []:
                    sender, text = parse_message(raw)
                    if not text:
                        continue

                    log.info(f"← {sender[:12]}: {text[:80]}")
                    reply = call_claude(sender, text)
                    send_message(client, sender, reply)
                    log.info(f"→ {sender[:12]}: {reply[:80]}")

                errors = 0
                time.sleep(0.5)

            except KeyboardInterrupt:
                log.info("已停止")
                break
            except Exception as e:
                errors += 1
                wait = min(30, errors * 3)
                log.error(f"主循环异常: {e}，{wait}s 后重试")
                time.sleep(wait)


if __name__ == "__main__":
    main()
