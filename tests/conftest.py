from io import BytesIO
from pathlib import Path

from docx import Document
import pytest

from ats.matching import parse_resume

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def resume_text():
    return (FIXTURES / "fictional_resume.txt").read_text(encoding="utf-8")


@pytest.fixture
def master(resume_text):
    return parse_resume(resume_text)


@pytest.fixture
def job_text():
    return (FIXTURES / "western_digital_job.txt").read_text(encoding="utf-8")


@pytest.fixture
def docx_bytes(resume_text):
    doc = Document()
    for line in resume_text.splitlines():
        doc.add_paragraph(line)
    output = BytesIO()
    doc.save(output)
    return output.getvalue()


# ---- Auto Apply fixtures (fictional applicant, mocked employer forms) -------
import os
from dataclasses import replace as _replace

AA_DESCRIPTION = """Responsibilities
Design and test printed circuit boards for wearable sensors.
Develop embedded firmware in C++ for ESP32 microcontrollers.
Debug hardware using an oscilloscope and multimeter.
Required
Experience with PCB design in KiCad.
Experience with C++ and I2C communication.
Must be authorized to work in the United States.
Preferred
FPGA experience with VHDL is a plus.
This Summer 2027 internship runs from May to August 2027 in our Atlanta laboratory."""


@pytest.fixture(autouse=True)
def _no_real_submission(monkeypatch):
    # Real submission can never be enabled by the environment during tests.
    for name in ("AUTOAPPLY_ALLOW_REAL_SUBMISSION", "AUTOAPPLY_EMERGENCY_STOP", "ANTHROPIC_API_KEY", "CLAUDE_ENABLED", "HUNTER_DB"):
        monkeypatch.delenv(name, raising=False)
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and Path("/opt/pw-browsers").is_dir():
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")


def aa_job(job_id="101", company="Fixture Co", title="Hardware Engineering Intern - Summer 2027", source="greenhouse", board="fixtureco", description=AA_DESCRIPTION, **changes):
    from hunter.models import Job
    url = f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}" if source == "greenhouse" else f"https://jobs.lever.co/{board}/{job_id}"
    job = Job(source, board, job_id, company, title, "Atlanta, GA", url, description,
              published_at="2026-10-22T12:00:00+00:00", publication_basis="Greenhouse first_published", checked_at="2026-10-23T12:00:00+00:00")
    return _replace(job, **changes)


def aa_profile(**changes):
    from autoapply.profile import ApplicantProfile
    base = ApplicantProfile(first_name="Alex", last_name="Example", email="alex@example.com", phone="(404) 555-0100", city="Atlanta", state="GA",
                            school="Example Institute of Technology", degree="Bachelor of Science", major="Electrical Engineering",
                            graduation="2028-05", gpa="3.68", us_citizen=None, work_authorized=True, sponsorship_needed=False,
                            available_terms=("Summer 2027",), github="https://github.com/example")
    return _replace(base, **changes)


class AAEnv:
    def __init__(self, tmp_path, master, router=None, **settings):
        from autoapply import config
        from autoapply.browser import BrowserFactory
        from autoapply.engine import Engine
        from autoapply.profile import save_profile, verify_profile
        from autoapply.selftest import FixtureRouter
        from hunter.storage import Store
        self.store = Store(tmp_path / "data" / "hunter.sqlite3")
        self.store.save_master(master)
        defaults = dict(db_path=self.store.path, min_fit_review=20.0, min_fit_auto=30.0, headless=True, browser_timeout_ms=15000)
        defaults.update(settings)
        self.settings = config.load(**defaults)
        self.router = router or FixtureRouter()
        self.engine = Engine(self.store, self.settings, browser=BrowserFactory(self.settings, self.router))
        self.auto = self.engine.auto
        save_profile(self.store, aa_profile())
        verify_profile(self.store, master)

    def add_job(self, job=None, at="2026-10-23T12:00:00+00:00"):
        job = job or aa_job()
        key, fresh = self.store.upsert(job, at)
        return key

    def permit(self, employer="greenhouse:fixtureco", auto_submit=False):
        self.auto.set_permission(employer, "Fixture Co", prefill=True, auto_submit=auto_submit, terms_reviewed=True)

    def switch_on(self, mode="REVIEW"):
        self.auto.set_enabled(True)
        if mode != "AUTO":
            self.auto.set_mode(mode)


@pytest.fixture
def aa(tmp_path, master):
    return AAEnv(tmp_path, master)


@pytest.fixture(scope="session")
def chromium_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch().close()
        return True
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Chromium for Playwright is not installed: {type(exc).__name__}")


# ---- SMS / gateway fixtures (official Twilio SDK over a fake HTTP transport) --------
import json as _json
import re as _re
import uuid as _uuid

TW_ACCOUNT = "AC" + "1" * 32
TW_TOKEN = "fixture-auth-token-0123456789abcdef"
TW_FROM = "+15005550006"
USER_PHONE = "+14045550123"
PUBLIC = "https://apply.example.test"
GATEWAY_PASSWORD = "correct horse battery staple"


class FakeTwilioHttp:
    """Stands in for twilio.http.HttpClient. The real SDK builds every request;
    nothing leaves the process. ``script`` queues responses or exceptions."""

    def __init__(self):
        self.requests, self.script = [], []
        self.is_async, self.timeout, self.last_request, self.last_response = False, None, None, None
        self.logger = None

    def request(self, method, url, params=None, data=None, headers=None, auth=None, timeout=None, allow_redirects=False):
        from twilio.http.response import Response
        self.requests.append({"method": method, "url": url, "data": dict(data or {}), "auth_user": (auth or ("",))[0]})
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return Response(201, _json.dumps({"sid": "SM" + _uuid.uuid4().hex, "status": "queued", "account_sid": TW_ACCOUNT}))

    @staticmethod
    def error(status, code):
        from twilio.http.response import Response
        return Response(status, _json.dumps({"code": code, "message": f"fixture error {code}", "status": status}))

    @property
    def bodies(self):
        return [r["data"].get("Body", "") for r in self.requests]


SMS_SETTINGS = dict(twilio_account_sid=TW_ACCOUNT, twilio_auth_token=TW_TOKEN, twilio_from_number=TW_FROM, public_base_url=PUBLIC,
                    gateway_secret_key="g" * 48, gateway_cookie_secure=False)


class SmsEnv(AAEnv):
    def __init__(self, tmp_path, master, router=None, **settings):
        from autoapply.notify import Notifier
        from autoapply.sms import SmsService
        from autoapply.webauth import hash_password
        merged = {**SMS_SETTINGS, "gateway_password_hash": hash_password(GATEWAY_PASSWORD, n=2 ** 12), **settings}
        super().__init__(tmp_path, master, router=router, **merged)
        self.twilio = FakeTwilioHttp()
        self.sms = SmsService(self.store, self.settings, http_client=self.twilio)
        self.notifier = Notifier(self.engine, self.sms)
        self._client = None

    def verify_phone(self, phone=USER_PHONE, enable=True):
        self.sms.start_verification(phone)
        code = _re.search(r"\b(\d{6})\b", self.twilio.bodies[-1])[1]
        self.sms.confirm_verification(code)
        if enable:
            self.sms.save_prefs({"enabled": True})
        return code

    @property
    def client(self):
        if self._client is None:
            from autoapply.gateway import create_app
            self._client = create_app(self.settings, self.store, self.engine, self.sms).test_client()
        return self._client

    def inbound(self, body, sid=None, sender=USER_PHONE, account=TW_ACCOUNT, signature=None, client=None):
        from twilio.request_validator import RequestValidator
        params = {"MessageSid": sid or "SM" + _uuid.uuid4().hex, "AccountSid": account, "From": sender, "To": TW_FROM, "Body": body, "NumMedia": "0"}
        sig = RequestValidator(TW_TOKEN).compute_signature(PUBLIC + "/sms/inbound", params) if signature is None else signature
        return (client or self.client).post("/sms/inbound", data=params, headers={"X-Twilio-Signature": sig})

    @staticmethod
    def reply(response):
        found = _re.search(r"<Message>(.*?)</Message>", response.get_data(as_text=True), _re.S)
        return found[1] if found else None

    def ready_application(self, job=None, approval_hash="hash-v1"):
        """A Ready for Review application without running a browser."""
        from autoapply import db as S
        key = self.add_job(job)
        app_id = self.auto.enqueue(key, job or aa_job(), "greenhouse:fixtureco", "dedupe-" + key[:8], "REVIEW",
                                   {"fit": 72.0, "checks": [{"name": "Fit threshold", "status": "pass", "detail": "Fit 72 ≥ 60.", "blocks": "auto"}]})
        self.auto.update(app_id, action="ready_for_review", state=S.READY, approval_hash=approval_hash, reasons=["Ready for your approval (REVIEW mode)."],
                         plan={"fit_before": 72.0, "fit_after": 75.0})
        return app_id

    def login(self):
        page = self.client.get("/login")
        csrf = _re.search(r'name="csrf" value="([^"]+)"', page.get_data(as_text=True))[1]
        return self.client.post("/login", data={"csrf": csrf, "password": GATEWAY_PASSWORD, "next": "/"})

    def csrf(self, path):
        return _re.search(r'name="csrf" value="([^"]+)"', self.client.get(path).get_data(as_text=True))[1]


@pytest.fixture
def smsenv(tmp_path, master):
    return SmsEnv(tmp_path, master)
