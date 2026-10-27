"""Regression checks for local configuration, pricing and office process failures."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

import runtime
from ats import docx_tailor
from autoapply import config
from autoapply.db import AutoStore
from autoapply.llm import Claude, LLMError, claude_status
from hunter.storage import Store


def test_local_env_loads_windows_bom_comments_and_preserves_exported_values(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text('RESUME_TEST_VALUE="local # value"\nRESUME_TEST_EXPORTED=local\nRESUME_TEST_EMPTY= # optional\n', encoding="utf-8-sig")
    for name in ("RESUME_TEST_VALUE", "RESUME_TEST_EMPTY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RESUME_TEST_EXPORTED", "exported")
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    runtime.load_environment(path)
    import os
    assert os.environ["RESUME_TEST_VALUE"] == "local # value"
    assert os.environ["RESUME_TEST_EMPTY"] == ""
    assert os.environ["RESUME_TEST_EXPORTED"] == "exported"


def test_env_is_loaded_from_installation_not_current_or_parent_directory(tmp_path, monkeypatch):
    installation, foreign = tmp_path / "installation", tmp_path / "foreign"
    installation.mkdir()
    foreign.mkdir()
    (installation / ".env").write_text("RESUME_TEST_LOCATION=installation\n")
    (foreign / ".env").write_text("RESUME_TEST_LOCATION=foreign\nAUTOAPPLY_ALLOW_REAL_SUBMISSION=true\n")
    monkeypatch.setattr(runtime, "ROOT", installation)
    monkeypatch.chdir(foreign)
    monkeypatch.delenv("RESUME_TEST_LOCATION", raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    runtime.load_environment()
    import os
    assert os.environ["RESUME_TEST_LOCATION"] == "installation"
    assert not config.load().allow_real_submission


def test_env_storage_path_is_read_before_hunter_selects_default_database(tmp_path):
    db_path = tmp_path / "persistent" / "jobs.sqlite3"
    (tmp_path / ".env").write_text("HUNTER_DB=" + db_path.as_posix() + "\n")
    import os
    env = {k: v for k, v in os.environ.items() if k not in {"HUNTER_DB", "PYTHON_DOTENV_DISABLED"}}
    script = "from pathlib import Path; import runtime; runtime.ROOT=Path(__import__('sys').argv[1]); from hunter.service import DEFAULT_DB; print(DEFAULT_DB)"
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=Path(__file__).resolve().parents[1],
                            env=env, capture_output=True, text=True, check=True, timeout=20)
    assert Path(result.stdout.strip()) == db_path
    assert not db_path.exists()  # Reading configuration never creates a database.


@pytest.mark.parametrize("raw", ["invalid", "nan", "inf", "-inf"])
def test_invalid_float_environment_values_use_safe_defaults(raw, monkeypatch):
    monkeypatch.setenv("CLAUDE_INPUT_USD_PER_MTOK", raw)
    monkeypatch.setenv("CLAUDE_DAILY_USD_LIMIT", raw)
    settings = config.load()
    assert settings.claude_input_usd_per_mtok is None and settings.claude_daily_usd == 2.0


@pytest.mark.parametrize("field,amount", [
    ("claude_input_usd_per_mtok", float("nan")), ("claude_input_usd_per_mtok", -3.0),
    ("claude_output_usd_per_mtok", 0.0), ("claude_output_usd_per_mtok", float("inf")),
    ("claude_daily_usd", float("nan")), ("claude_monthly_usd", -1.0),
])
def test_invalid_price_or_spending_limit_cannot_send_a_paid_request(tmp_path, field, amount):
    settings = config.load(db_path=tmp_path / "db", claude_enabled=True, anthropic_api_key="fixture-key", claude_model="fixture-model",
                           claude_input_usd_per_mtok=3.0, claude_output_usd_per_mtok=15.0)
    settings = replace(settings, **{field: amount})
    client = Mock()
    claude = Claude(AutoStore(Store(settings.db_path)), settings, client=client)
    with pytest.raises(LLMError):
        claude.structured("fixture", "system", {}, {"type": "object"})
    client.messages.create.assert_not_called()


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -1.0, 0.0])
def test_sidebar_readiness_rejects_invalid_prices(price):
    assert not claude_status(True, "fixture-key", "fixture-model", price, 15)[0]


def test_sidebar_renders_with_malformed_price_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_INPUT_USD_PER_MTOK", "not a price")
    monkeypatch.setenv("CLAUDE_OUTPUT_USD_PER_MTOK", "nan")
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=40)
    app.session_state["hunter_db_path"] = str(tmp_path / "db")
    app.run()
    assert not app.exception
    prices = {x.label: x.value for x in app.number_input}
    assert prices["Input $/M tokens"] == prices["Output $/M tokens"] == 0.0


def test_sidebar_uses_server_secret_file_without_exposing_its_contents(tmp_path, monkeypatch):
    secret = tmp_path / "key.txt"
    secret.write_text("fixture-secret-not-real")
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", str(secret))
    monkeypatch.setenv("CLAUDE_MODEL", "fixture-model")
    monkeypatch.setenv("CLAUDE_INPUT_USD_PER_MTOK", "3")
    monkeypatch.setenv("CLAUDE_OUTPUT_USD_PER_MTOK", "15")
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=40)
    app.session_state["hunter_db_path"] = str(tmp_path / "db")
    app.run()
    app.toggle[0].set_value(True).run()
    assert not app.exception and any("ready (fixture-model)" in x.value for x in app.success)
    assert all("fixture-secret-not-real" not in x.value for x in app.text_input)


@pytest.mark.parametrize("fault", ["timeout", "crash"])
def test_word_fault_is_isolated_and_libreoffice_fallback_still_works(monkeypatch, fault):
    monkeypatch.setattr(docx_tailor, "sys", SimpleNamespace(platform="win32", executable=sys.executable))
    monkeypatch.setattr(docx_tailor.shutil, "which", lambda name: "soffice")
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        if str(args[1]).endswith("word_pdf.py"):
            if fault == "timeout":
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            Path(args[-1]).write_bytes(b"partial failed Word output")
            return SimpleNamespace(returncode=1)
        assert not (Path(args[-1]).parent / "resume.pdf").exists()
        (Path(args[-1]).parent / "resume.pdf").write_bytes(b"%PDF-fallback-fixture")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(docx_tailor.subprocess, "run", run)
    pdf, converter = docx_tailor.docx_to_pdf(b"fixture", timeout=2)
    assert pdf == b"%PDF-fallback-fixture" and converter == "LibreOffice" and len(calls) == 2
    assert all(0 < options["timeout"] <= 2 for _, options in calls)
    assert any(x.startswith("-env:UserInstallation=file:") for x in calls[1][0])


def test_parallel_libreoffice_conversions_use_separate_profiles(monkeypatch):
    monkeypatch.setattr(docx_tailor, "sys", SimpleNamespace(platform="linux", executable=sys.executable))
    monkeypatch.setattr(docx_tailor.shutil, "which", lambda name: "soffice")
    profiles = []
    def run(args, **kwargs):
        profiles.append(next(x for x in args if x.startswith("-env:UserInstallation=")))
        (Path(args[-1]).parent / "resume.pdf").write_bytes(b"%PDF-fixture")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(docx_tailor.subprocess, "run", run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(docx_tailor.docx_to_pdf, [b"first", b"second"]))
    assert len(set(profiles)) == 2 and all(pdf for pdf, converter in results)
