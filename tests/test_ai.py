"""独立 AI Blueprint 合约测试；上游全部用 MockTransport，不使用真实密钥。"""
import base64
from datetime import datetime
from io import BytesIO
import json
import socket
from zoneinfo import ZoneInfo

from flask import Flask
import httpx
from PIL import Image
import pytest

import ai


FAKE_KEY = "test-only-not-a-real-api-key"
HEADERS = {"X-AI-Key": FAKE_KEY}
FIXED_NOW = datetime(2026, 10, 12, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def completion(content):
    return httpx.Response(200, json={
        "id": "test-completion", "object": "chat.completion", "created": 1,
        "model": "test-model", "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": content}}],
    })


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(ai.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))])
    state = {"calls": [], "response": completion("# 整理后的笔记")}

    def upstream(outgoing):
        state["calls"].append(outgoing)
        result = state["response"]
        if isinstance(result, Exception):
            raise result
        return result

    app = Flask(__name__)
    app.config.update(TESTING=True, TZ="Asia/Shanghai", NOW_PROVIDER=lambda: FIXED_NOW,
                      AI_HTTP_TRANSPORT_FACTORY=lambda: httpx.MockTransport(upstream))
    ai.init_app(app)
    return app.test_client(), state, app


def test_organize_forwards_ephemeral_header_key_with_pinned_ip_and_original_tls_name(service):
    client, state, app = service
    response = client.post("/api/ai/organize", json={"content": "零散笔记"}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json == {"data": {"content": "# 整理后的笔记"}}
    outgoing = state["calls"][0]
    assert outgoing.url.host == "93.184.216.34"
    assert outgoing.headers["Host"] == "api.deepseek.com"
    assert outgoing.extensions["sni_hostname"] == "api.deepseek.com"
    assert outgoing.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert outgoing.headers["Accept-Encoding"] == "identity"
    body = json.loads(outgoing.content)
    assert body["model"] == "deepseek-v4.1-flash"
    assert FAKE_KEY not in outgoing.content.decode()
    assert FAKE_KEY not in response.get_data(as_text=True)
    assert FAKE_KEY not in str(app.config)


def test_task_parsing_includes_now_and_normalizes_confirmable_dates(service):
    client, state, _ = service
    state["response"] = completion(json.dumps({"tasks": [
        {"title": "交报告", "due_at": "2026-10-21T23:59:00+08:00"},
        {"title": "看书", "due_at": None},
    ]}, ensure_ascii=False))
    response = client.post("/api/ai/tasks", json={"text": "下周三前交报告；看书"}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json["data"]["tasks"][0]["due_at"] == "2026-10-21T15:59:00+00:00"
    assert response.json["data"]["tasks"][1]["due_at"] is None
    prompt = json.loads(state["calls"][0].content)["messages"][0]["content"]
    assert "2026-10-12T12:00:00+08:00" in prompt
    assert "Asia/Shanghai" in prompt


def test_cards_accepts_json_code_fence_and_does_not_write_data(service):
    client, state, _ = service
    state["response"] = completion('```json\n{"cards":[{"front":"问题？","back":"答案"}]}\n```')
    response = client.post("/api/ai/cards", json={"content": "资料"}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json["data"]["cards"] == [{"front": "问题？", "back": "答案"}]


@pytest.mark.parametrize("output", [
    "not json", "[]", '{"tasks":null}', '{"tasks":{}}',
    '{"tasks":[{"title":"作业"}]}',
    '{"tasks":[{"title":true,"due_at":null}]}',
    '{"tasks":[{"title":"作业","due_at":"2026-02-31T12:00:00+08:00"}]}',
    '{"tasks":[{"title":"作业","due_at":"2026-10-13"}]}',
    '{"tasks":[{"title":"作业","due_at":"1900-10-13T12:00:00Z"}]}',
    '{"tasks":[{"title":"作业","due_at":true}]}',
    json.dumps({"tasks": [{"title": "作业", "due_at": None}] * 21}),
])
def test_invalid_upstream_task_schema_is_rejected(service, output):
    client, state, _ = service
    state["response"] = completion(output)
    result = client.post("/api/ai/tasks", json={"text": "任务"}, headers=HEADERS)
    assert result.status_code == 502


@pytest.mark.parametrize("address", [
    "http://api.deepseek.com", "https://api.deepseek.com:8443", "https://127.0.0.1",
    "https://[::1]", "https://user:password@api.deepseek.com", "https://evil.example",
    "https://api.deepseek.com?key=example", "https://api.deepseek.com#fragment",
    "https://api.deepseek.com.evil.example", "https://api.deepseek.com/path?",
])
def test_unsafe_service_addresses_never_make_a_request(service, address):
    client, state, _ = service
    result = client.post("/api/ai/organize", json={"content": "笔记"}, headers={**HEADERS, "X-AI-Base-URL": address})
    assert result.status_code == 400
    assert state["calls"] == []


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "ff02::1", "64:ff9b::7f00:1", "224.0.0.1"])
def test_mixed_public_and_forbidden_dns_is_rejected(service, monkeypatch, address):
    client, state, _ = service
    monkeypatch.setattr(ai.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))])
    result = client.post("/api/ai/organize", json={"content": "笔记"}, headers=HEADERS)
    assert result.status_code == 400
    assert not state["calls"]


def test_redirect_to_metadata_is_not_followed(service):
    client, state, _ = service
    state["response"] = httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
    result = client.post("/api/ai/organize", json={"content": "笔记"}, headers=HEADERS)
    assert result.status_code == 502
    assert len(state["calls"]) == 1


@pytest.mark.parametrize("status, expected", [(401, 502), (403, 502), (429, 429), (500, 502)])
def test_upstream_error_body_and_key_are_never_returned_or_logged(service, caplog, status, expected):
    client, state, _ = service
    state["response"] = httpx.Response(status, json={"error": {"message": FAKE_KEY, "type": "service_error"}})
    result = client.post("/api/ai/organize", json={"content": "笔记"}, headers=HEADERS)
    assert result.status_code == expected
    assert FAKE_KEY not in result.get_data(as_text=True)
    assert FAKE_KEY not in caplog.text
    assert len(state["calls"]) == 1


def test_timeout_is_friendly_and_does_not_retry(service):
    client, state, _ = service
    state["response"] = httpx.ReadTimeout(FAKE_KEY)
    result = client.post("/api/ai/organize", json={"content": "笔记"}, headers=HEADERS)
    assert result.status_code == 504
    assert FAKE_KEY not in result.get_data(as_text=True)
    assert len(state["calls"]) == 1


def test_response_size_is_bounded(service):
    client, state, _ = service
    state["response"] = httpx.Response(200, content=b"x" * (ai.MAX_RESPONSE_BYTES + 1))
    result = client.post("/api/ai/organize", json={"content": "笔记"}, headers=HEADERS)
    assert result.status_code == 502


@pytest.mark.parametrize("payload", [None, [], {"content": ""}, {"content": True}, {"content": "x" * 100001}])
def test_invalid_content_never_calls_upstream(service, payload):
    client, state, _ = service
    result = client.post("/api/ai/organize", json=payload, headers=HEADERS)
    assert result.status_code == 400
    assert not state["calls"]


def jpeg_data(size=(40, 30), format="JPEG"):
    output = BytesIO()
    Image.new("RGB", size, color="white").save(output, format=format)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()


def test_vision_sends_validated_jpeg_in_openai_multimodal_format(service):
    client, state, _ = service
    image = jpeg_data()
    result = client.post("/api/ai/vision", json={"image": image}, headers=HEADERS)
    assert result.status_code == 200
    messages = json.loads(state["calls"][0].content)["messages"]
    assert messages[1]["content"][1] == {"type": "image_url", "image_url": {"url": image}}
    assert "LaTeX" in messages[0]["content"] and "- [ ]" in messages[0]["content"]


@pytest.mark.parametrize("image", ["data:image/jpeg;base64,INVALID!", jpeg_data(format="PNG"), jpeg_data(size=(2201, 2))])
def test_invalid_images_are_rejected_before_upstream(service, image):
    client, state, _ = service
    result = client.post("/api/ai/vision", json={"image": image}, headers=HEADERS)
    assert result.status_code == 400
    assert not state["calls"]


def test_missing_key_cannot_be_supplied_in_json_body(service):
    client, state, _ = service
    result = client.post("/api/ai/organize", json={"content": "笔记", "api_key": FAKE_KEY})
    assert result.status_code == 400
    assert not state["calls"]


def test_allowed_custom_path_is_kept_and_proxy_environment_is_ignored(service, monkeypatch):
    client, state, _ = service
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    response = client.post("/api/ai/organize", json={"content": "笔记"}, headers={
        **HEADERS, "X-AI-Base-URL": "https://api.openai.com/v1", "X-AI-Model": "custom-model"})
    assert response.status_code == 200
    outgoing = state["calls"][0]
    assert outgoing.url.path == "/v1/chat/completions"
    assert outgoing.headers["Host"] == outgoing.extensions["sni_hostname"] == "api.openai.com"
    assert json.loads(outgoing.content)["model"] == "custom-model"


def test_pinned_transport_rejects_attempt_to_change_origin():
    calls = []
    transport = ai.PinnedTransport("api.deepseek.com", "93.184.216.34", httpx.MockTransport(lambda outgoing: calls.append(outgoing)))
    with pytest.raises(ai.AIUnavailable):
        transport.handle_request(httpx.Request("GET", "https://evil.example/path"))
    assert calls == []


def test_dns_rebinding_cannot_trigger_second_lookup(service, monkeypatch):
    client, state, _ = service
    count = 0

    def resolve(*args, **kwargs):
        nonlocal count
        count += 1
        address = "93.184.216.34" if count == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]

    monkeypatch.setattr(ai.socket, "getaddrinfo", resolve)
    response = client.post("/api/ai/organize", json={"content": "笔记"}, headers=HEADERS)
    assert response.status_code == 200
    assert count == 1
    assert state["calls"][0].url.host == "93.184.216.34"


def test_compressed_upstream_body_is_rejected_before_decompression(service):
    client, state, _ = service
    state["response"] = httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not-read"))
    result = client.post("/api/ai/organize", json={"content": "笔记"}, headers=HEADERS)
    assert result.status_code == 502


def test_chunked_response_without_length_still_has_size_limit(service):
    client, state, _ = service

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * ai.MAX_RESPONSE_BYTES
            yield b"y"

    state["response"] = httpx.Response(200, stream=Stream())
    result = client.post("/api/ai/organize", json={"content": "笔记"}, headers=HEADERS)
    assert result.status_code == 502


def test_truncated_jpeg_is_rejected(service):
    client, state, _ = service
    image = jpeg_data()
    prefix, encoded = image.split(",", 1)
    broken = prefix + "," + base64.b64encode(base64.b64decode(encoded)[:-40]).decode()
    result = client.post("/api/ai/vision", json={"image": broken}, headers=HEADERS)
    assert result.status_code == 400
    assert not state["calls"]
