from dataclasses import asdict, replace
from unittest.mock import Mock
import json

import pytest

from ats.matching import extract_requirements
from ats.models import Change
from ats.rewriting import (ModelConfig, apply_changes, parse_changes, rewrite_request, tailor_once, validate_change)
from ats.scoring import analyze
from ats.models import Extracted


def pcb_bullet(master):
    return next(x for x in master.items if x.kind == "bullet" and "four-layer" in x.text)


@pytest.mark.parametrize("mutation", [
    lambda s: s + " using STM32.",
    lambda s: s.replace("3.3 V", "5 V"),
    lambda s: s + " Improved performance by 30%.",
    lambda s: s.replace("Designed and built", "Planned"),
    lambda s: s.replace("built", "led"),
    lambda s: s.replace("KiCad", "Altium"),
    lambda s: s + " Patented a revolutionary technology.",
    lambda s: s.replace("3.3 V", "3.3 A"),
])
def test_unsupported_claim_rejection(master, mutation):
    source = pcb_bullet(master)
    change = Change((source.id,), source.text, mutation(source.text), "test")
    assert validate_change(change, master).status == "rejected"


def test_alias_and_conservative_reordering_require_approval(master):
    source = pcb_bullet(master)
    after = "Designed and built a four-layer wearable PCB with 3.3 V power rails in KiCad."
    change = Change((source.id,), source.text, after, "Put hardware outcome first")
    assert validate_change(change, master).status == "review"
    assert apply_changes(master, master, (change,), set()) == master
    updated = apply_changes(master, master, (change,), {source.id})
    assert updated.by_id[source.id].text == after
    assert master.by_id[source.id].text == source.text
    assert all(updated.by_id[x.id] == x for x in master.items if x.kind != "bullet")
    assert "3.3 V" in updated.text and "GPA: 3.68" in updated.text


def test_rejected_edit_cannot_be_applied(master):
    source = pcb_bullet(master)
    change = Change((source.id,), source.text, source.text + " Achieved 100% reliability.", "test")
    with pytest.raises(ValueError, match="Rejected"):
        apply_changes(master, master, (change,), {source.id})


def test_original_text_and_provenance_checked(master):
    source = pcb_bullet(master)
    assert validate_change(Change((source.id,), "different original", source.text, "test"), master).status == "rejected"
    assert validate_change(Change((source.id, "other"), source.text, source.text, "test"), master).status == "rejected"
    with pytest.raises(ValueError, match="unselected"):
        parse_changes({"changes": [{"source_ids": [source.id], "original_text": source.text, "proposed_text": source.text, "justification": "test"}]}, {"different"})


def test_bounded_rewrite_request_excludes_application_history(master, job_text):
    source = pcb_bullet(master)
    request = rewrite_request(master, extract_requirements(job_text), [source.id])
    body = json.dumps(request)
    assert len(body) < 18000 and "alex@example.com" not in body
    evidence = request["data"]["evidence"]
    assert [(e["id"], e["text"]) for e in evidence] == [(source.id, source.text)]
    # Same-entry context lets the model use the project's verified facts, never contact details.
    assert evidence[0]["same_entry_context"] and all("@" not in c for c in evidence[0]["same_entry_context"])
    assert "source_ids" in request["response_schema"]["properties"]["changes"]["items"]["properties"]


def test_no_unintended_llm_calls_and_cache(master, job_text):
    source = pcb_bullet(master)
    transport = Mock(return_value={"payload": {"changes": []}, "usage": {"total_tokens": 120}})
    cache = {}
    arguments = (master, job_text, extract_requirements(job_text), [source.id], ModelConfig("configured-model"), "fake-key")
    analyze(master, Extracted(master.text, "demo"), arguments[2])
    rewrite_request(master, arguments[2], [source.id])
    assert transport.call_count == 0
    for intentional, enabled, key in [(False, True, "x"), (True, False, "x"), (True, True, "")]:
        with pytest.raises(ValueError):
            tailor_once(*arguments[:-1], key, intentional=intentional, enabled=enabled, cache=cache, transport=transport)
    assert transport.call_count == 0
    first = tailor_once(*arguments, intentional=True, enabled=True, cache=cache, transport=transport)
    again = tailor_once(*arguments, intentional=True, enabled=True, cache=cache, transport=transport)
    assert transport.call_count == 1 and not first["cache_hit"] and again["cache_hit"]
    changed = (*arguments[:4], ModelConfig("configured-model", 1900), arguments[-1])
    tailor_once(*changed, intentional=True, enabled=True, cache=cache, transport=transport)
    assert transport.call_count == 2
    assert "fake-key" not in str(cache)


def test_failed_or_uncertain_calls_are_not_retried(master, job_text):
    source = pcb_bullet(master)
    transport = Mock(side_effect=TimeoutError("network"))
    cache = {}
    for _ in range(2):
        with pytest.raises(ValueError):
            tailor_once(master, job_text, (), [source.id], ModelConfig("configured-model"), "fake-key", intentional=True, enabled=True, cache=cache, transport=transport)
    assert transport.call_count == 1


def test_http_adapter_is_one_bounded_structured_request(master, monkeypatch):
    from ats.rewriting import call_openai
    source = pcb_bullet(master)
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.status_code = 200
    response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": '{"changes": []}'}}], "usage": {"total_tokens": 25}}
    http = Mock(return_value=response)
    monkeypatch.setattr("ats.rewriting.requests.post", http)
    result = call_openai(rewrite_request(master, (), [source.id]), ModelConfig("configured-model", 1000), "test-key")
    assert result["payload"] == {"changes": []} and http.call_count == 1
    args = http.call_args.kwargs
    assert args["json"]["store"] is False
    assert args["json"]["max_completion_tokens"] == 1000
    assert args["json"]["response_format"]["json_schema"]["strict"] is True
    response.json.return_value["choices"][0]["finish_reason"] = "length"
    with pytest.raises(ValueError, match="incomplete"):
        call_openai(rewrite_request(master, (), [source.id]), ModelConfig("configured-model"), "test-key")
