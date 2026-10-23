"""Claude integration with a fake SDK client: no network, no spend."""
from types import SimpleNamespace
import json

import pytest

from conftest import aa_job
from ats.matching import extract_requirements
from autoapply import config
from autoapply.db import AutoStore
from autoapply.llm import BudgetExceeded, Claude, LLMError, validate_cover_letter
from hunter.storage import Store


def message(payload, stop="end_turn", input_tokens=1000, output_tokens=200):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens))


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def claude_env(tmp_path):
    store = Store(tmp_path / "h.sqlite3")
    settings = config.load(db_path=store.path, claude_enabled=True, anthropic_api_key="sk-ant-test-not-real", claude_model="claude-test-model",
                           claude_input_usd_per_mtok=3.0, claude_output_usd_per_mtok=15.0, claude_daily_usd=1.0, claude_monthly_usd=5.0, claude_max_retries=2)
    return store, AutoStore(store), settings


SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


def test_unconfigured_without_prices_or_key(tmp_path):
    store = Store(tmp_path / "h.sqlite3")
    s = config.load(db_path=store.path, claude_enabled=True, anthropic_api_key="k", claude_model="m")
    ok, why = Claude(AutoStore(store), s).configured()
    assert not ok and "USD" in why
    assert not Claude(AutoStore(store), config.load(db_path=store.path)).configured()[0]


def test_structured_output_schema_cache_and_usage(claude_env):
    store, auto, settings = claude_env
    client = FakeClient(message({"ok": True}))
    c = Claude(auto, settings, client=client)
    first = c.structured("t", "sys", {"x": 1}, SCHEMA)
    second = c.structured("t", "sys", {"x": 1}, SCHEMA)
    assert first["payload"] == {"ok": True} and not first["cache_hit"] and second["cache_hit"]
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["output_config"]["format"]["schema"] == SCHEMA and call["model"] == "claude-test-model"
    assert "untrusted" in call["system"] and call["messages"][0]["content"].startswith("<data>")
    row = auto.llm_usage()[0]
    assert row["status"] == "ok" and row["input_tokens"] == 1000 and abs(row["cost_usd"] - (1000 * 3 + 200 * 15) / 1e6) < 1e-9
    assert "sk-ant-test" not in store.path.read_bytes().decode("latin1")


def test_schema_violation_is_not_retried_and_is_cached(claude_env):
    _, auto, settings = claude_env
    client = FakeClient(message({"ok": "yes"}), message({"ok": True}))
    c = Claude(auto, settings, client=client)
    with pytest.raises(LLMError):
        c.structured("t", "sys", {"x": 2}, SCHEMA)
    with pytest.raises(LLMError, match="Cached failure"):
        c.structured("t", "sys", {"x": 2}, SCHEMA)
    assert len(client.calls) == 1
    c.clear_failure("t")
    assert c.structured("t", "sys", {"x": 2}, SCHEMA)["payload"] == {"ok": True}


def test_transient_errors_retry_with_a_bound(claude_env):
    _, auto, settings = claude_env
    sleeps = []
    client = FakeClient(ConnectionError("reset"), ConnectionError("reset"), ConnectionError("reset"), message({"ok": True}))
    c = Claude(auto, settings, client=client, sleep=sleeps.append)
    with pytest.raises(LLMError):
        c.structured("t", "sys", {"x": 3}, SCHEMA)
    assert len(client.calls) == settings.claude_max_retries + 1 and len(sleeps) == settings.claude_max_retries
    client2 = FakeClient(ConnectionError("reset"), message({"ok": True}))
    assert Claude(auto, settings, client=client2, sleep=sleeps.append).structured("t", "sys", {"x": 4}, SCHEMA)["payload"]["ok"]


def test_truncated_response_rejected_without_retry(claude_env):
    _, auto, settings = claude_env
    client = FakeClient(message("{\"ok\": tr", stop="max_tokens"))
    with pytest.raises(LLMError, match="stopped early"):
        Claude(auto, settings, client=client).structured("t", "sys", {"x": 5}, SCHEMA)
    assert len(client.calls) == 1


def test_spending_and_token_limits_refuse_before_sending(claude_env):
    store, auto, _ = claude_env
    tight = config.load(db_path=store.path, claude_enabled=True, anthropic_api_key="k", claude_model="m",
                        claude_input_usd_per_mtok=3.0, claude_output_usd_per_mtok=15.0, claude_daily_usd=0.001, claude_monthly_usd=5.0)
    client = FakeClient(message({"ok": True}))
    with pytest.raises(BudgetExceeded):
        Claude(auto, tight, client=client).structured("t", "sys", {"x": 6}, SCHEMA)
    small = config.load(db_path=store.path, claude_enabled=True, anthropic_api_key="k", claude_model="m",
                        claude_input_usd_per_mtok=3.0, claude_output_usd_per_mtok=15.0, claude_max_input_tokens=1000)
    with pytest.raises(BudgetExceeded):
        Claude(auto, small, client=client).structured("t", "sys", {"x": "y" * 10000}, SCHEMA)
    assert client.calls == []


def test_rewrite_transport_keeps_phase1_evidence_validation(claude_env, master):
    from ats.rewriting import ModelConfig, tailor_once, validate_change
    from hunter.service import RewriteCache
    store, auto, settings = claude_env
    bullet = next(x for x in master.items if "four-layer" in x.text)
    fabricated = {"changes": [{"source_ids": [bullet.id], "original_text": bullet.text,
                               "proposed_text": "Designed and built a six-layer wearable PCB in Altium with 5 V rails, cutting power 50%.", "justification": "x"}]}
    client = FakeClient(message(fabricated))
    c = Claude(auto, settings, client=client)
    job = aa_job()
    result = tailor_once(master, job.description, extract_requirements(job.description), [bullet.id], ModelConfig("claude-test-model"),
                         "managed", intentional=True, enabled=True, cache=RewriteCache(store), transport=c.rewrite_transport())
    change = result["changes"][0]
    verdict = validate_change(change, master)
    assert verdict.status == "rejected"  # invented metrics/tools never pass


def test_question_mapping_never_sends_profile_values(claude_env):
    _, auto, settings = claude_env
    client = FakeClient(message({"mappings": [{"question_id": "q1", "source": "profile", "key": "school", "confidence": "high"},
                                              {"question_id": "q2", "source": "profile", "key": "salary", "confidence": "high"},
                                              {"question_id": "q3", "source": "profile", "key": "email", "confidence": "low"}]}))
    c = Claude(auto, settings, client=client)
    qs = [{"id": f"q{n}", "label": f"Question {n}", "kind": "text"} for n in (1, 2, 3)]
    mappings, _ = c.map_questions(qs, ["school", "email"], [])
    assert [m["question_id"] for m in mappings] == ["q1"]
    sent = client.calls[0]["messages"][0]["content"]
    assert "Example Institute" not in sent and "alex@example.com" not in sent


def test_cover_letter_validation_rejects_unsupported_claims(master):
    bullet = next(x for x in master.items if "four-layer" in x.text)
    job = aa_job()
    good = [{"text": "I designed and built a four-layer wearable PCB in KiCad with 3.3 V power rails.", "evidence_ids": [bullet.id]}]
    assert validate_cover_letter(good, master, job) == []
    bad = [{"text": "I designed PCBs in Altium and FPGA firmware achieving 40% savings.", "evidence_ids": [bullet.id]}]
    problems = validate_cover_letter(bad, master, job)
    assert any("Altium" in p or "FPGA" in p for p in problems) and any("40%" in p for p in problems)
