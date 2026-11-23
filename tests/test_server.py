"""OpenAI-kompatibler Server mit Fake-Modellen."""

import base64
import json

from fastapi.testclient import TestClient

from eth_agent.agent.core import Agent
from eth_agent.indexer.build import index_course
from eth_agent.llm.providers import FakeProvider
from eth_agent.llm.usage import UsageLog
from eth_agent.server import clean_assistant, create_app, is_helper_task

from test_check import grade_json
from test_indexer import course_dir, make_cfg  # noqa: F401

KEY = "geheim"
H = {"Authorization": f"Bearer {KEY}"}


def _client(cfg, **providers):
    agent = Agent(cfg, providers=providers, usage=UsageLog(cfg.data_dir / "usage.sqlite"))
    index_course(cfg, agent.store, cfg.courses["physik2"], log=lambda *_: None)
    return TestClient(create_app(cfg, agent, KEY)), agent


def _stream_text(resp) -> str:
    out = []
    for line in resp.iter_lines():
        if line.startswith("data: ") and line != "data: [DONE]":
            delta = json.loads(line[6:])["choices"][0]["delta"]
            out.append(delta.get("content", ""))
    return "".join(out)


def test_auth_and_models(course_dir):  # noqa: F811
    client, _ = _client(make_cfg(course_dir), codex=FakeProvider("codex"))
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer falsch"}).status_code == 401
    data = client.get("/v1/models", headers=H).json()["data"]
    assert [m["id"] for m in data] == ["eth-agent"]
    assert client.get("/health").json()["status"] == "ok"


def test_chat_non_stream(course_dir):  # noqa: F811
    codex = FakeProvider("codex", reply="Der Tunneleffekt ... [1]")
    client, _ = _client(make_cfg(course_dir), codex=codex)
    r = client.post("/v1/chat/completions", headers=H, json={
        "model": "eth-agent", "stream": False,
        "messages": [{"role": "system", "content": "ignoriert"},
                     {"role": "user", "content": "Was ist Spin?"},
                     {"role": "assistant", "content": "<think>\n· alt\n</think>\n\nSpin ist ...\n\n---\n**Quellen**\n[1] x"},
                     {"role": "user", "content": "Erkläre den Tunneleffekt"}]})
    msg = r.json()["choices"][0]["message"]["content"]
    assert "Der Tunneleffekt" in msg and "Modell: codex" in msg
    prompt = codex.prompts[-1]
    assert "## Question (verbatim)\n\nErkläre den Tunneleffekt" in prompt
    assert "Spin ist ..." in prompt and "· alt" not in prompt and "**Quellen**" not in prompt


def test_chat_stream_with_status(course_dir):  # noqa: F811
    client, _ = _client(make_cfg(course_dir), codex=FakeProvider("codex", reply="Antwort mit $x^2$"))
    with client.stream("POST", "/v1/chat/completions", headers=H, json={
            "model": "eth-agent", "stream": True,
            "messages": [{"role": "user", "content": "Berechne die Johnson noise"}]}) as r:
        text = _stream_text(r)
    assert text.startswith("<think>\n· Suche im Kursmaterial")
    assert "</think>\n\nAntwort mit $x^2$" in text


def test_helper_tasks_cost_nothing(course_dir):  # noqa: F811
    codex = FakeProvider("codex")
    client, _ = _client(make_cfg(course_dir), codex=codex)
    r = client.post("/v1/chat/completions", headers=H, json={
        "model": "eth-agent", "stream": False,
        "messages": [{"role": "user", "content": "Erkläre den Tunneleffekt bitte genau"},
                     {"role": "user", "content": "### Task:\nGenerate a concise, 3-5 word title ..."}]})
    title = json.loads(r.json()["choices"][0]["message"]["content"])["title"]
    assert title.startswith("Erkläre den Tunneleffekt") and codex.prompts == []
    assert is_helper_task("### Task:\nGenerate 1-3 broad tags")


def test_check_with_uploaded_image(course_dir):  # noqa: F811
    codex = FakeProvider("codex", reply=grade_json(reference_agreement="official"))
    client, _ = _client(make_cfg(course_dir), codex=codex, claude=FakeProvider("claude"))
    img = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff fake jpeg").decode()
    r = client.post("/v1/chat/completions", headers=H, json={
        "model": "eth-agent", "stream": False,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "/check Prüfung 2024 Aufgabe 2"},
            {"type": "image_url", "image_url": {"url": img}}]}]})
    out = r.json()["choices"][0]["message"]["content"]
    assert "Bewertet von: codex" in out and "Korrektur" in out


def test_clean_assistant():
    assert clean_assistant("<think>\n· a\n</think>\n\nText\n\n---\n**Quellen**\n[1] y") == "Text"
