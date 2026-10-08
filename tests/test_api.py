# -*- coding: utf-8 -*-
"""Проверка HTTP-слоя бота против локального фальшивого сервера MAX."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
import bot


class Fake(BaseHTTPRequestHandler):
    log = []
    plan = {}          # (method, path) -> список ответов (status, body)

    def _do(self, method):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        Fake.log.append({"m": method, "path": u.path, "q": parse_qs(u.query), "body": body,
                         "auth": self.headers.get("Authorization")})
        queue = Fake.plan.get((method, u.path)) or [(200, {})]
        status, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if status == 429:
            self.send_header("Retry-After", "0")
        self.end_headers()
        self.wfile.write(raw)

    do_GET = lambda s: s._do("GET")
    do_POST = lambda s: s._do("POST")
    do_PUT = lambda s: s._do("PUT")
    do_PATCH = lambda s: s._do("PATCH")

    def log_message(self, *a):
        pass


@pytest.fixture
def api():
    Fake.log, Fake.plan = [], {}
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield bot.MaxAPI("TOKEN123", base=f"http://127.0.0.1:{srv.server_port}")
    srv.shutdown()


def test_auth_header_and_update_types(api):
    api.get_updates(marker=5, timeout=1)
    r = Fake.log[-1]
    assert r["auth"] == "TOKEN123"
    assert r["q"]["types"] == [bot.UPDATE_TYPES] and r["q"]["marker"] == ["5"]
    assert "access_token" not in r["q"]


def test_send_targets_chat_or_user(api):
    api.send(chat_id=7, text="x")
    api.send(user_id=9, text="y")
    assert Fake.log[0]["q"] == {"chat_id": ["7"]}
    assert Fake.log[1]["q"] == {"user_id": ["9"]}


def test_retry_on_429(api):
    Fake.plan[("POST", "/answers")] = [(429, {}), (200, {"ok": True})]
    api.answer_callback("cb", notification="hi")
    assert len([x for x in Fake.log if x["path"] == "/answers"]) == 2


def test_retry_when_attachment_not_ready(api, monkeypatch):
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    Fake.plan[("POST", "/messages")] = [(400, {"code": "attachment.not.ready"}), (200, {"ok": 1})]
    api.send(chat_id=1, text="f", attachments=[{"type": "file", "payload": {"token": "t"}}])
    assert len([x for x in Fake.log if x["path"] == "/messages"]) == 2


def test_other_http_errors_propagate(api):
    import requests
    Fake.plan[("POST", "/messages")] = [(403, {"code": "forbidden"})]
    with pytest.raises(requests.HTTPError):
        api.send(chat_id=1, text="x")


def test_callback_answer_carries_notification_and_message_together(api):
    kbd = bot.kb([[("a", "b")]])
    api.answer_callback("cb1", notification="ok", text="hello", keyboard=kbd)
    body = Fake.log[-1]["body"]
    assert body["notification"] == "ok" and body["message"]["text"] == "hello"
    assert body["message"]["attachments"][0]["type"] == "inline_keyboard"
