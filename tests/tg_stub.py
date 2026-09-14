#!/usr/bin/env python3
"""Telegram Bot API 的最小桩服务（用于容器冒烟测试，不联网、不碰真 Token）。

用法::

    python tg_stub.py --port 8099 --log /tmp/stub.log [--expect-token XXX] \
                      [--script-after N --script-chat 123456 --script-text /status]

行为：
  * 校验 URL 里的 bot token 与 --expect-token 一致，否则回 401（模拟真 Telegram）；
  * 前 N 次 getUpdates 返回空，之后返回一条脚本化的指令更新
    （这样应用启动时"丢弃积压更新"的那一步不会把脚本吃掉）；
  * 所有收到的调用按行写进 --log，供测试断言。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {
    "getupdates_calls": 0,
    "updates_delivered": False,
    "calls": [],
    "script_after": 3,
    "script_chat": 0,
    "script_text": "",
    "expect_token": "",
    "log_path": "",
}
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # 静音，避免污染测试输出
        pass

    # ---------------------------------------------------------------- #
    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {"_raw": raw.decode("utf-8", "replace")}

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parts = [p for p in self.path.split("/") if p]
        token = ""
        method = parts[-1] if parts else ""
        for part in parts:
            if part.startswith("bot"):
                token = part[3:]
        body = self._read_body()

        if STATE["expect_token"] and token != STATE["expect_token"]:
            self._record({"method": method, "status": 401, "text": None})
            self._reply({"ok": False, "error_code": 401,
                         "description": "Unauthorized"}, status=401)
            return

        result = self._handle(method, body)
        self._record({"method": method, "status": 200,
                      "chat_id": body.get("chat_id"), "text": body.get("text")})
        self._reply({"ok": True, "result": result})

    def _record(self, entry):
        """逐条即时落盘：即使进程被 SIGKILL，已有调用也不会丢。"""
        with LOCK:
            STATE["calls"].append(entry)
            if STATE["log_path"]:
                with open(STATE["log_path"], "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ---------------------------------------------------------------- #
    def _handle(self, method, body):
        if method == "getMe":
            return {"id": 123456789, "is_bot": True, "first_name": "CloudOps",
                    "username": "cloudops_smoketest_bot", "can_join_groups": True,
                    "can_read_all_group_messages": False, "supports_inline_queries": False}

        if method == "getUpdates":
            with LOCK:
                STATE["getupdates_calls"] += 1
                n = STATE["getupdates_calls"]
                if n >= STATE["script_after"] and STATE["script_text"] and not STATE["updates_delivered"]:
                    STATE["updates_delivered"] = True
                    return [{
                        "update_id": 1000 + n,
                        "message": {
                            "message_id": 7,
                            "date": int(time.time()),
                            "chat": {"id": STATE["script_chat"], "type": "private"},
                            "from": {"id": STATE["script_chat"], "is_bot": False,
                                     "first_name": "Admin", "language_code": "zh"},
                            "text": STATE["script_text"],
                        },
                    }]
            return []

        if method in ("sendMessage", "editMessageText"):
            return {"message_id": 1, "date": int(time.time()),
                    "chat": {"id": body.get("chat_id") or 0, "type": "private"},
                    "text": body.get("text") or ""}

        if method == "getWebhookInfo":
            return {"url": "", "has_custom_certificate": False, "pending_update_count": 0}

        if method in ("setMyCommands", "deleteWebhook", "setWebhook", "sendChatAction",
                      "answerCallbackQuery", "deleteMessage"):
            return True

        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram API 桩")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--log", default="")
    parser.add_argument("--expect-token", default="")
    parser.add_argument("--script-after", type=int, default=3)
    parser.add_argument("--script-chat", type=int, default=0)
    parser.add_argument("--script-text", default="")
    args = parser.parse_args()

    STATE.update(script_after=args.script_after, script_chat=args.script_chat,
                 script_text=args.script_text, expect_token=args.expect_token,
                 log_path=args.log)
    if args.log:
        open(args.log, "w", encoding="utf-8").close()   # 每次启动清空，断言不残留

    server = ThreadingHTTPServer((args.host, args.port), Handler)

    print(f"stub listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
