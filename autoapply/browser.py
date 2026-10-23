"""Playwright form automation for employer-hosted candidate application pages.

Only the candidate-facing HTML form is used, exactly as a person would. No
private ATS APIs, no credentials, no cookies are persisted (each run uses a
fresh browser context), and any CAPTCHA, login, MFA, access restriction or
unexpected page stops the run for human action. Page content is data: it is
never interpreted as instructions.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import re
from urllib.parse import urlsplit

from ats.models import digest

from .forms import FormField, Resolution

# ---- field discovery ------------------------------------------------------

DISCOVER_JS = r"""
(rootSel) => {
  const root = (rootSel && document.querySelector(rootSel)) || document.querySelector('form') || document.body;
  const text = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const visible = el => { const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return el.type === 'file' || (s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0)); };
  let n = 0;
  const tag = el => { if (!el.dataset.aaid) el.dataset.aaid = 'f' + (n++); return `[data-aaid="${el.dataset.aaid}"]`; };
  const labelFor = el => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const by = el.getAttribute('aria-labelledby');
    if (by) return by.split(/\s+/).map(id => text(document.getElementById(id))).join(' ');
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return text(l); }
    const wrap = el.closest('label'); if (wrap) return text(wrap);
    const box = el.closest('.field, .application-question, .form-field, li, div');
    if (box) { const l = box.querySelector('label, .application-label, .label'); if (l) return text(l); }
    return el.getAttribute('placeholder') || el.name || '';
  };
  const isRequired = (el, label) => el.required || el.getAttribute('aria-required') === 'true' || /[*✱]\s*$|\(required\)/i.test(label);
  const fields = [];
  const seenGroups = new Set();
  for (const el of root.querySelectorAll('input, select, textarea')) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type) || el.disabled) continue;
    if (!visible(el)) continue;
    if (type === 'radio' || type === 'checkbox') {
      const group = el.name ? root.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`) : [el];
      if (type === 'radio' || group.length > 1) {
        if (seenGroups.has(el.name)) continue; seenGroups.add(el.name);
        const fs = el.closest('fieldset');
        const legend = fs ? text(fs.querySelector('legend')) : '';
        const options = Array.from(group).map(o => labelFor(o));
        const label = legend || (el.closest('[role=radiogroup], [role=group]') ? (el.closest('[role=radiogroup], [role=group]').getAttribute('aria-label') || '') : '') || el.name;
        const req = Array.from(group).some(o => o.required) || (fs && fs.getAttribute('aria-required') === 'true') || /[*✱]\s*$/.test(label);
        fields.push({fid: `input[name="${el.name}"]`, label, kind: type === 'radio' ? 'radio' : 'checkbox_group', required: !!req, options, name: el.name});
        continue;
      }
      const label = labelFor(el);
      fields.push({fid: tag(el), label, kind: 'checkbox', required: isRequired(el, label), options: [], name: el.name || ''});
      continue;
    }
    const label = labelFor(el);
    let kind = el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : (el.getAttribute('role') === 'combobox' ? 'combobox' : type);
    if (!['text','email','tel','url','number','date','file','select','textarea','combobox'].includes(kind)) kind = 'text';
    const options = el.tagName === 'SELECT' ? Array.from(el.options).filter(o => o.value !== '').map(o => o.text.trim()) : [];
    fields.push({fid: tag(el), label, kind, required: isRequired(el, label), options, name: el.name || '', accept: el.getAttribute('accept') || ''});
  }
  return fields;
}
"""

CAPTCHA_SELECTORS = ("iframe[src*='recaptcha']", "iframe[src*='hcaptcha']", "iframe[src*='challenges.cloudflare.com']", "iframe[src*='arkoselabs']",
                     "iframe[src*='funcaptcha']", "iframe[title*='challenge' i]", "[data-sitekey]", ".g-recaptcha", ".h-captcha", ".cf-turnstile",
                     "script[src*='recaptcha']", "script[src*='hcaptcha']", "script[src*='turnstile']")
LOGIN_SELECTORS = ("input[type='password']", "input[autocomplete='one-time-code']")
TEXT_BLOCKERS = (
    ("captcha", r"verify (that )?you are (a )?human|are you a robot|complete the (security )?challenge|captcha"),
    ("login", r"sign in to (continue|apply)|log in to (continue|apply)|create an account to apply|please (sign|log) in"),
    ("mfa", r"verification code|one[- ]time (pass)?code|two[- ]factor|multi[- ]factor|enter the code (we|sent)"),
    # Kept narrow: job descriptions legitimately mention "rate limits" etc.
    ("access", r"access denied|403 forbidden|too many requests|unusual traffic from your|you have been blocked|request (was )?blocked"),
    ("closed", r"no longer accepting applications|position has been filled|job (is )?no longer available|job not found|this job (posting )?has expired|page not found"),
)
CONFIRMATION = re.compile(r"thank(s| you) for (applying|your application|your interest)|application (has been |was )?(submitted|received)|we('ve| have) received your application|successfully (submitted|applied)", re.I)
ERROR_SELECTORS = ("[aria-invalid='true']", ".error:not(:empty)", ".field-error:not(:empty)", ".invalid-feedback:not(:empty)", "[role='alert']:not(:empty)")


@dataclass
class Blocker:
    kind: str
    detail: str


@dataclass
class RunReport:
    url: str
    adapter: str
    fields: list[FormField] = field(default_factory=list)
    resolutions: list[Resolution] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    filled: list[str] = field(default_factory=list)
    fill_errors: list[str] = field(default_factory=list)
    screenshot: str = ""
    submitted: bool = False
    outcome: str = "not_submitted"  # not_submitted, confirmed, unknown
    confirmation: dict = field(default_factory=dict)

    @property
    def field_set_hash(self):
        return digest([f.signature() for f in self.fields])


# ---- adapters ---------------------------------------------------------------

class Adapter:
    name = "base"
    hosts: tuple[str, ...] = ()
    form_selectors: tuple[str, ...] = ("form",)
    submit_selectors: tuple[str, ...] = ("button[type=submit]", "input[type=submit]")
    open_form_selectors: tuple[str, ...] = ()
    confirm_url = re.compile(r"/confirmation|/thanks|/thank-you|/submitted", re.I)

    def handles(self, url):
        host = (urlsplit(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def apply_url(self, job):  # pragma: no cover - overridden
        raise NotImplementedError

    def allowed_host(self, url):
        return self.handles(url)


class GreenhouseAdapter(Adapter):
    """Greenhouse hosted job boards (job-boards.greenhouse.io / boards.greenhouse.io)."""
    name = "greenhouse"
    hosts = ("job-boards.greenhouse.io", "boards.greenhouse.io")
    form_selectors = ("form#application-form", "form#application_form", "#application-form form", "form[action*='applications']", "form")
    submit_selectors = ("button[type=submit]:has-text('Submit')", "#submit_app", "button:has-text('Submit application')", "input[type=submit]")
    open_form_selectors = ("button:has-text('Apply')", "a:has-text('Apply for this job')")

    def apply_url(self, job):
        if job.source == "greenhouse" and re.fullmatch(r"[A-Za-z0-9_-]+", job.board) and re.fullmatch(r"\d+", str(job.source_id)):
            return f"https://job-boards.greenhouse.io/{job.board}/jobs/{job.source_id}"
        m = re.match(r"https://(?:job-boards|boards)\.greenhouse\.io/([A-Za-z0-9_-]+)/jobs/(\d+)", job.url)
        return f"https://job-boards.greenhouse.io/{m[1]}/jobs/{m[2]}" if m else None


class LeverAdapter(Adapter):
    """Lever hosted postings (jobs.lever.co/{company}/{id}/apply)."""
    name = "lever"
    hosts = ("jobs.lever.co",)
    form_selectors = ("form#application-form", "form.application-form", "form[action*='/apply']", "form")
    submit_selectors = ("button#btn-submit", "button[type=submit]:has-text('Submit')", "button:has-text('Submit application')", "input[type=submit]")

    def apply_url(self, job):
        if job.source == "lever" and re.fullmatch(r"[A-Za-z0-9_-]+", job.board) and re.fullmatch(r"[A-Za-z0-9-]+", str(job.source_id)):
            return f"https://jobs.lever.co/{job.board}/{job.source_id}/apply"
        m = re.match(r"https://jobs\.lever\.co/([A-Za-z0-9_-]+)/([A-Za-z0-9-]+)", job.url)
        return f"https://jobs.lever.co/{m[1]}/{m[2]}/apply" if m else None


ADAPTERS = (GreenhouseAdapter(), LeverAdapter())


def adapter_for(job):
    for adapter in ADAPTERS:
        url = adapter.apply_url(job)
        if url:
            return adapter, url
    return None, None


# ---- browser session ------------------------------------------------------

class BrowserFactory:
    """Fresh, cookie-less Chromium context per application.

    ``router`` (tests and dry runs) intercepts *every* request, so a mocked
    run can never reach a real employer site.
    """

    def __init__(self, settings, router=None):
        self.settings, self.router = settings, router

    @contextmanager
    def page(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            options = {"headless": self.settings.headless}
            if self.settings.chromium_executable:
                options["executable_path"] = self.settings.chromium_executable
            browser = pw.chromium.launch(**options)
            try:
                context = browser.new_context(accept_downloads=False, java_script_enabled=True, service_workers="block")
                if self.router is not None:
                    context.route("**/*", self.router)
                page = context.new_page()
                page.set_default_timeout(self.settings.browser_timeout_ms)
                yield page
                context.close()
            finally:
                browser.close()


def detect_blockers(page, adapter, response_status=None) -> list[Blocker]:
    found = []
    if response_status and response_status >= 400:
        found.append(Blocker("access", f"Application page returned HTTP {response_status}."))
    if not adapter.allowed_host(page.url):
        found.append(Blocker("redirect", "Navigation left the supported application host: " + (urlsplit(page.url).hostname or "?")))
    for selector in CAPTCHA_SELECTORS:
        if page.locator(selector).count():
            found.append(Blocker("captcha", "Security challenge present (" + selector + "). Not bypassed."))
            break
    for selector in LOGIN_SELECTORS:
        if page.locator(selector).count():
            found.append(Blocker("login", "Login or one-time code field present."))
            break
    try:
        body = page.locator("body").inner_text(timeout=5000)[:20000]
    except Exception:
        body = ""
    for kind, pattern in TEXT_BLOCKERS:
        if re.search(pattern, body, re.I) and not any(b.kind == kind for b in found):
            found.append(Blocker(kind, "Page text indicates: " + kind))
    return found


def _form_root(page, adapter):
    for selector in adapter.form_selectors:
        if page.locator(selector).count():
            return selector
    for selector in adapter.open_form_selectors:
        button = page.locator(selector).first
        if button.count():
            button.click()
            page.wait_for_timeout(500)
            for form_selector in adapter.form_selectors:
                if page.locator(form_selector).count():
                    return form_selector
    return None


def discover(page, root):
    return [FormField(**row) for row in page.evaluate(DISCOVER_JS, root)]


def fill(page, resolutions) -> tuple[list[str], list[str]]:
    filled, errors = [], []
    for r in resolutions:
        if not r.filled:
            continue
        try:
            locator = page.locator(r.fid)
            if r.kind == "file":
                locator.first.set_input_files(str(r.value))
            elif r.kind == "select":
                locator.first.select_option(label=str(r.value))
            elif r.kind == "radio":
                group = page.locator(r.fid)
                target = None
                for i in range(group.count()):
                    option = group.nth(i)
                    label = page.evaluate("(el) => { const l = el.id && document.querySelector(`label[for='${CSS.escape(el.id)}']`); return (l ? l.innerText : (el.closest('label') ? el.closest('label').innerText : el.value)).trim(); }", option.element_handle())
                    if label.strip() == str(r.value).strip():
                        target = option
                        break
                if target is None:
                    raise ValueError("option not found")
                target.check()
            elif r.kind == "checkbox":
                locator.first.set_checked(bool(r.value))
            elif r.kind == "combobox":
                box = locator.first
                box.click()
                box.fill(str(r.value))
                option = page.locator("[role=option]", has_text=str(r.value)).first
                option.click()
            else:
                locator.first.fill(str(r.value))
            filled.append(r.fid)
        except Exception as exc:  # recorded; a required unfilled field blocks submission
            errors.append(f"{r.label[:80]}: could not fill ({type(exc).__name__})")
    return filled, errors


def wait_for_outcome(page, adapter, before_url, timeout_ms=20000):
    """Applied requires explicit confirmation; anything else is Unknown."""
    deadline = timeout_ms
    step = 500
    while deadline > 0:
        try:
            body = page.locator("body").inner_text(timeout=3000)[:20000]
        except Exception:
            body = ""
        match = CONFIRMATION.search(body)
        url_match = adapter.confirm_url.search(urlsplit(page.url).path) and page.url != before_url
        if match or url_match:
            return "confirmed", {"url": page.url, "matched_text": match.group(0) if match else "", "url_pattern": bool(url_match),
                                 "excerpt": body[max(0, (match.start() if match else 0) - 120):(match.end() if match else 0) + 240]}
        page.wait_for_timeout(step)
        deadline -= step
    errors = []
    for selector in ERROR_SELECTORS:
        loc = page.locator(selector)
        for i in range(min(loc.count(), 5)):
            try:
                errors.append(loc.nth(i).inner_text(timeout=1000)[:200] or selector)
            except Exception:
                errors.append(selector)
    return "unknown", {"url": page.url, "errors": errors, "note": "No confirmation message or confirmation URL observed."}


def run_application(page, adapter, url, resolve_fn, evidence_dir: Path, before_submit=None) -> RunReport:
    """Open, discover, resolve, fill; optionally submit after ``before_submit``.

    ``resolve_fn(fields) -> resolutions``. ``before_submit(report) -> bool`` is
    the engine's final gate (stop switch, caps, approval hash, idempotent
    claim). Submission happens only if it returns True.
    """
    evidence_dir.mkdir(parents=True, exist_ok=True)
    report = RunReport(url=url, adapter=adapter.name)
    response = page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    report.blockers = detect_blockers(page, adapter, response.status if response else None)
    if report.blockers:
        report.screenshot = _shot(page, evidence_dir, "blocked")
        return report
    root = _form_root(page, adapter)
    if not root:
        report.blockers.append(Blocker("form", "No application form found on the candidate page."))
        report.screenshot = _shot(page, evidence_dir, "no-form")
        return report
    report.fields = discover(page, root)
    report.resolutions = resolve_fn(report.fields)
    report.filled, report.fill_errors = fill(page, report.resolutions)
    # Re-check: some challenges appear only after interaction.
    late = [b for b in detect_blockers(page, adapter) if b.kind in ("captcha", "login", "mfa", "redirect")]
    report.blockers.extend(late)
    report.screenshot = _shot(page, evidence_dir, "filled")
    if before_submit is None or report.blockers or report.fill_errors:
        return report
    if not before_submit(report):
        return report
    button = None
    for selector in adapter.submit_selectors:
        candidate = page.locator(root + " " + selector).first if page.locator(root + " " + selector).count() else page.locator(selector).first
        if candidate.count():
            button = candidate
            break
    if button is None:
        report.outcome = "unknown"
        report.confirmation = {"error": "Submit button not found after the submission claim; nothing was clicked."}
        return report
    before_url = page.url
    report.submitted = True
    try:
        button.click()
    except Exception as exc:
        report.outcome, report.confirmation = "unknown", {"error": f"Click raised {type(exc).__name__}; outcome uncertain."}
        report.confirmation["screenshot"] = _shot(page, evidence_dir, "after-submit")
        return report
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    report.outcome, report.confirmation = wait_for_outcome(page, adapter, before_url)
    report.confirmation["screenshot"] = _shot(page, evidence_dir, "confirmation" if report.outcome == "confirmed" else "after-submit")
    report.confirmation["html"] = _save_html(page, evidence_dir)
    return report


def _shot(page, folder, name):
    path = folder / f"{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception:
        return ""


def _save_html(page, folder):
    path = folder / "after-submit.html"
    try:
        path.write_text(page.content(), encoding="utf-8")
        return str(path)
    except Exception:
        return ""
