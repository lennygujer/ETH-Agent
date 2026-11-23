"""OpenAI-kompatibler Server für Open WebUI.

Open WebUI schickt bei jeder Anfrage den ganzen Verlauf; der Server ist zustandslos und ruft
dieselbe handle()-Funktion auf wie der Terminal-Chat.

- GET  /v1/models            -> ein Modell pro Kurs ("eth-agent" bzw. "eth-agent-<kurs>")
- POST /v1/chat/completions  -> Antwort (Streaming mit Statuszeilen oder als Ganzes)
- GET  /health
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import queue
import re
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .agent.core import Agent, Turn
from .config import Config

KEEPALIVE_S = 15
_THINK = re.compile(r"<think>.*?</think>\s*", re.S)
_DETAILS = re.compile(r"<details[^>]*>.*?</details>\s*", re.S)
_SOURCES = re.compile(r"\n-{3,}\n\*\*Quellen\*\*.*\Z", re.S)
_DATA_URL = re.compile(r"^data:image/(png|jpe?g|webp|gif);base64,(.+)$", re.S)

# Open WebUI nutzt das gewählte Modell auch für Hilfsaufgaben (Titel, Tags, Folgefragen,
# Suchanfragen). Diese sollen keine Cloud-Aufrufe kosten.
_TASK_MARKERS = ("### Task:", "Generate a concise", "Generate 1-3 broad tags", "follow-up questions",
                 "Analyze the chat history to determine the necessity of generating search queries",
                 "autocomplete")


def model_ids(cfg: Config) -> dict[str, str | None]:
    # Ein Modell; das Fach wählt man im Chat mit /physik, /analysis, ... (siehe /kurse)
    return {"eth-agent": None}


def _text_and_images(content: Any, tmpdir: Path) -> tuple[str, list[str]]:
    """OpenAI-Content (String oder Liste mit text/image_url) -> Text und Bilddateien."""
    if isinstance(content, str):
        return content, []
    texts, images = [], []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            texts.append(part.get("text", ""))
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            m = _DATA_URL.match(url)
            if not m:
                continue
            try:
                data = base64.b64decode(m.group(2), validate=False)
            except (binascii.Error, ValueError):
                continue
            ext = "jpg" if m.group(1) in {"jpg", "jpeg"} else m.group(1)
            path = tmpdir / f"bild_{len(images) + 1}.{ext}"
            path.write_bytes(data)
            images.append(str(path))
    return "\n".join(texts), images


def clean_assistant(text: str) -> str:
    """Statusblock und Quellenliste aus früheren Antworten entfernen (spart Tokens im Verlauf)."""
    text = _THINK.sub("", text)
    text = _DETAILS.sub("", text)
    text = _SOURCES.sub("", text)
    return text.strip()


def is_helper_task(text: str) -> bool:
    return any(m in text for m in _TASK_MARKERS)


def helper_reply(text: str, history: list[Turn]) -> str:
    """Billige Antworten auf Open-WebUI-Hilfsaufgaben, ohne Modell."""
    first_user = next((t.text for t in history if t.role == "user"), "")
    if "title" in text.lower():
        words = re.sub(r"[`*#>\[\]]", "", first_user).split()
        return json.dumps({"title": " ".join(words[:6]) or "Neuer Chat"}, ensure_ascii=False)
    if "tags" in text.lower():
        return json.dumps({"tags": ["ETH", "Lernen"]})
    if "follow" in text.lower():
        return json.dumps({"follow_ups": []})
    if "queries" in text.lower():
        return json.dumps({"queries": []})
    return ""


def _chunk(cid: str, model: str, delta: dict, finish: str | None = None) -> str:
    payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def create_app(cfg: Config, agent: Agent, api_key: str | None) -> FastAPI:
    app = FastAPI(title="ETH-Agent", version="0.1")
    ids = model_ids(cfg)
    # Eine Anfrage nach der anderen: ein Benutzer, eine SQLite-Verbindung, Abo-Kontingent schonen.
    busy = threading.Lock()

    def auth(authorization: str | None = Header(default=None)) -> None:
        if not api_key:
            return
        token = (authorization or "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(token, api_key):
            raise HTTPException(status_code=401, detail="Ungültiger API-Schlüssel")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "courses": list(cfg.courses),
                "providers": {n: p.available() for n, p in agent.providers.items()}}

    @app.get("/v1/models", dependencies=[Depends(auth)])
    def models() -> dict:
        return {"object": "list", "data": [
            {"id": mid, "object": "model", "created": 0, "owned_by": "eth-agent",
             "name": "ETH-Agent"} for mid in ids]}

    @app.post("/v1/chat/completions", dependencies=[Depends(auth)])
    def chat(body: dict):
        model = body.get("model") or "eth-agent"
        course_id = ids.get(model)
        messages = body.get("messages") or []
        stream = bool(body.get("stream"))
        tmp = Path(tempfile.mkdtemp(prefix="eth-agent-req-"))

        history: list[Turn] = []
        last_text, last_images = "", []
        for i, m in enumerate(messages):
            role = m.get("role")
            if role not in {"user", "assistant"}:
                continue
            text, images = _text_and_images(m.get("content"), tmp)
            if i == len(messages) - 1 and role == "user":
                last_text, last_images = text, images
            else:
                history.append(Turn(role, clean_assistant(text) if role == "assistant" else text))
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        if is_helper_task(last_text):
            reply = helper_reply(last_text, history)
            return _respond(cid, model, reply, stream, status=[])

        if not stream:
            try:
                with busy:
                    ans = agent.handle(last_text, course_id=course_id, history=history, images=last_images)
            finally:
                _cleanup(tmp)
            return _respond(cid, model, ans.render(), False, status=ans.status)

        q: queue.Queue = queue.Queue()

        def work() -> None:
            try:
                if not busy.acquire(blocking=False):
                    q.put(("status", "Warte auf die vorherige Anfrage …"))
                    busy.acquire()
                try:
                    ans = agent.handle(last_text, course_id=course_id, history=history, images=last_images,
                                       on_status=lambda s: q.put(("status", s)))
                finally:
                    busy.release()
                q.put(("answer", ans.render()))
            except Exception as e:  # Fehler als Antwort anzeigen statt Verbindung abzubrechen
                q.put(("answer", f"Interner Fehler: {type(e).__name__}: {e}"))
            finally:
                _cleanup(tmp)
                q.put(("done", None))

        threading.Thread(target=work, daemon=True).start()

        def events() -> Iterator[str]:
            yield _chunk(cid, model, {"role": "assistant", "content": ""})
            think_open = False
            while True:
                try:
                    kind, val = q.get(timeout=KEEPALIVE_S)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if kind == "status":
                    if not think_open:
                        yield _chunk(cid, model, {"content": "<think>\n"})
                        think_open = True
                    yield _chunk(cid, model, {"content": f"· {val}\n"})
                elif kind == "answer":
                    if think_open:
                        yield _chunk(cid, model, {"content": "</think>\n\n"})
                        think_open = False
                    yield _chunk(cid, model, {"content": val})
                elif kind == "done":
                    yield _chunk(cid, model, {}, finish="stop")
                    yield "data: [DONE]\n\n"
                    return

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


def _cleanup(tmp: Path) -> None:
    for f in tmp.glob("*"):
        f.unlink(missing_ok=True)
    tmp.rmdir()


def _respond(cid: str, model: str, text: str, stream: bool, status: list[str]):
    if stream:
        def one() -> Iterator[str]:
            yield _chunk(cid, model, {"role": "assistant", "content": text})
            yield _chunk(cid, model, {}, finish="stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(one(), media_type="text/event-stream")
    return JSONResponse({
        "id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


def api_key_from_env(cfg: Config) -> str | None:
    """Schlüssel aus $ETH_AGENT_API_KEY oder <repo>/.env (Zeile ETH_AGENT_API_KEY=...)."""
    key = os.environ.get("ETH_AGENT_API_KEY")
    if key:
        return key.strip()
    from .config import REPO_ROOT
    env = REPO_ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ETH_AGENT_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    return None
