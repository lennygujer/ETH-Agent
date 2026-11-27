"""Verbrauchsprotokoll: Token-Zahlen aus Codex/Claude, Zuordnung pro Nachricht, /verbrauch."""

import json

from eth_agent.agent.core import Agent
from eth_agent.llm.providers import FakeProvider, LLMResult, codex_usage
from eth_agent.llm.usage import UsageLog, parse_days

from test_indexer import course_dir, make_cfg  # noqa: F401  (Fixture)


def test_codex_usage_new_format():
    out = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "x"}),
        "kein json",
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 800,
                                                         "output_tokens": 50, "reasoning_output_tokens": 30}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 200, "output_tokens": 10}}),
    ])
    assert codex_usage(out) == {"input_tokens": 1200, "cached_input_tokens": 800, "output_tokens": 60,
                                "reasoning_tokens": 30}


def test_codex_usage_old_format():
    ev = lambda n: json.dumps({"id": "0", "msg": {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": n, "cached_input_tokens": 0, "output_tokens": 7}}}})
    assert codex_usage(ev(100) + "\n" + ev(300)) == {"input_tokens": 300, "cached_input_tokens": 0,
                                                      "output_tokens": 7}
    assert codex_usage("") == {}


def test_requests_and_report(tmp_path):
    log = UsageLog(tmp_path / "u.sqlite")
    with log.request("/check") as rid:
        log.record(LLMResult("codex", "a", True, 1.0, input_tokens=5000, cached_input_tokens=4000,
                             output_tokens=500, reasoning_tokens=300), skill="check-solve", rule="check",
                   prompt_tokens_est=0)
        log.record(LLMResult("claude", "b", True, 1.0, input_tokens=3000, output_tokens=400, cost_usd=0.05),
                   skill="check-solve", rule="check", prompt_tokens_est=0)
    log.finish_request(rid, fallback="/check", course="physik2")
    with log.request() as rid2:
        pass
    log.finish_request(rid2, fallback="/lernkarte")
    cmds = {c: (n, per) for c, n, per in log.by_command(log.since(1))}
    assert cmds["/check"][0] == 1 and set(cmds["/check"][1]) == {"codex", "claude"}
    assert log.message_count(log.since(1)) == (2, 1)
    text = log.report(1)
    assert "Codex (ChatGPT Plus)" in text and "5.0k (4.0k)" in text and "500 (300)" in text
    assert "/check" in text and "0.05 USD" in text and "1 ohne Cloud-Aufruf" in text


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "u.sqlite")
    db.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now')), provider TEXT, "
               "skill TEXT, route_rule TEXT, prompt_tokens_est INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
               "cost_usd REAL, duration_s REAL, ok INTEGER, rate_limited INTEGER, error TEXT)")
    db.execute("INSERT INTO calls (provider, skill, input_tokens, output_tokens, ok) VALUES ('codex','answer',10,2,1)")
    db.commit()
    log = UsageLog(tmp_path / "u.sqlite")
    assert log.by_provider(log.since(1))["codex"].input_tokens == 10
    assert log.by_command(log.since(1))[0][0] == "Frage"


def test_parse_days():
    assert (parse_days(""), parse_days("heute"), parse_days("30"), parse_days("monat")) == (7, 1, 30, 30)


def test_verbrauch_command_in_chat(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    agent = Agent(cfg, providers={"codex": FakeProvider("codex"), "claude": FakeProvider("claude")},
                  usage=UsageLog(cfg.data_dir / "usage.sqlite"))
    agent.handle("Was ist Spin?")
    ans = agent.handle("/verbrauch heute")
    assert ans.route.skill == "verbrauch" and not agent.providers["claude"].prompts
    assert len(agent.providers["codex"].prompts) == 1            # /verbrauch selbst ohne Cloud-Aufruf
    assert "| Frage | 1 |" in ans.render()
