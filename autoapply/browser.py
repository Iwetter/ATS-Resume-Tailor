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

# Label of one radio/checkbox option. Shared by discovery and filling so the
# option text compared at fill time is exactly the text that was resolved.
OPTION_LABEL_JS = r"""(el) => {
  const text = n => (n ? (n.innerText || n.textContent || '') : '').replace(/\s+/g, ' ').trim();
  if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').replace(/\s+/g, ' ').trim();
  if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return text(l); }
  const wrap = el.closest('label'); if (wrap) return text(wrap);
  return (el.value || '').trim();
}"""

DISCOVER_JS = r"""
(rootSel) => {
  const root = (rootSel && document.querySelector(rootSel)) || document.querySelector('form') || document.body;
  const text = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const optionLabel = """ + OPTION_LABEL_JS + r""";
  const visible = el => { const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return el.type === 'file' || (s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0)); };
  let n = 0;
  const tag = el => { if (!el.dataset.aaid) el.dataset.aaid = 'f' + (n++); return `[data-aaid="${el.dataset.aaid}"]`; };
  const byIds = ids => ids.split(/\s+/).map(id => text(document.getElementById(id))).join(' ').trim();
  // Question text from the nearest question container (Greenhouse upload groups,
  // Lever cards, generic .field wrappers). Never a label that wraps another input.
  const questionLabel = el => {
    let node = el.parentElement;
    for (let depth = 0; node && node !== root && depth < 7; depth++, node = node.parentElement) {
      const role = node.getAttribute('role') || '';
      if (/group/.test(role) && node.getAttribute('aria-labelledby')) { const t = byIds(node.getAttribute('aria-labelledby')); if (t) return t; }
      if (/group/.test(role) && node.getAttribute('aria-label')) return node.getAttribute('aria-label').trim();
      if (node.tagName === 'FIELDSET') { const t = text(node.querySelector('legend')); if (t) return t; }
      if (node.matches('.application-question, .field, .form-field, .question, li')) {
        const l = Array.from(node.querySelectorAll('.application-label, legend, label, .label')).find(c => !c.querySelector('input, select, textarea') && !c.classList.contains('visually-hidden') && text(c));
        if (l) return text(l);
      }
    }
    return '';
  };
  const labelFor = el => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    if (el.getAttribute('aria-labelledby')) { const t = byIds(el.getAttribute('aria-labelledby')); if (t) return t; }
    if (el.type === 'file') { const q = questionLabel(el); if (q) return q; }  // "Attach" buttons are not the question
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l && text(l)) return text(l); }
    const wrap = el.closest('label');
    if (wrap) { const inner = wrap.querySelector('.application-label'); return text(inner || wrap); }
    return questionLabel(el) || el.getAttribute('placeholder') || el.name || '';
  };
  const marked = label => /[*✱]\s*$|\(required\)/i.test(label);
  const isRequired = (el, label) => el.required || el.getAttribute('aria-required') === 'true' || marked(label) ||
    !!el.closest('[role=group][aria-required=true], .required-field');
  const fields = [];
  const seenGroups = new Set();
  for (const el of root.querySelectorAll('input, select, textarea')) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type) || el.disabled) continue;
    // React-Select keeps an invisible required <input aria-hidden tabindex=-1> next to each
    // combobox for native validation; it is not a question of its own.
    if (type !== 'file' && el.getAttribute('aria-hidden') === 'true' && el.tabIndex < 0) continue;
    if (!visible(el)) continue;
    if (type === 'radio' || type === 'checkbox') {
      const group = el.name ? root.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`) : [el];
      if (type === 'radio' || group.length > 1) {
        if (seenGroups.has(el.name)) continue; seenGroups.add(el.name);
        const options = Array.from(group).map(o => optionLabel(o));
        const label = questionLabel(el) || el.name;
        const req = Array.from(group).some(o => o.required) || !!el.closest('[aria-required=true], .required-field') || marked(label);
        fields.push({fid: `input[name="${CSS.escape(el.name)}"]`, label, kind: type === 'radio' ? 'radio' : 'checkbox_group', required: !!req, options, name: el.name});
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

# Options of an open combobox listbox (React-Select and ARIA 1.2 comboboxes).
COMBOBOX_OPTIONS_JS = r"""(sel) => {
  const el = document.querySelector(sel);
  if (!el) return [];
  const id = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
  const box = (id && document.getElementById(id)) || el.closest('.select-shell, .select, [class*=container]')?.querySelector('[role=listbox], [class*=menu]');
  if (!box) return [];
  return Array.from(box.querySelectorAll('[role=option]')).map(o => (o.innerText || o.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
}"""

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


def _form_root(page, adapter, open_form=True):
    for selector in adapter.form_selectors:
        if page.locator(selector).count():
            return selector
    for selector in adapter.open_form_selectors if open_form else ():
        button = page.locator(selector).first
        if button.count():
            button.click()
            page.wait_for_timeout(500)
            for form_selector in adapter.form_selectors:
                if page.locator(form_selector).count():
                    return form_selector
    return None


def combobox_options(page, fid, limit=300):
    """Open a combobox's menu, read its options, close it. Nothing is typed or chosen.

    Searchable comboboxes (city, school) list nothing until text is typed; they
    return [] and are matched against the real options at fill time instead.
    """
    box = page.locator(fid).first
    try:
        box.focus(timeout=3000)
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(250)
        return page.evaluate(COMBOBOX_OPTIONS_JS, fid)[:limit]
    except Exception:
        return []
    finally:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass


def discover(page, root):
    fields = [FormField(**row) for row in page.evaluate(DISCOVER_JS, root)]
    for f in fields:
        if f.kind == "combobox" and not f.options:
            f.options = [o for o in combobox_options(page, f.fid) if not re.fullmatch(r"(?i)no options|loading\W*|type to search\W*", o)]
    return fields


def _option_text(page, locator):
    return re.sub(r"\s+", " ", page.evaluate(OPTION_LABEL_JS, locator.element_handle())).strip()


def _pick_combobox(page, box, value):
    """Type the verified value, then click the one option that matches it exactly
    (or uniquely contains it). No match → error → Needs Human Action."""
    from .forms import _choose
    box.click()
    box.fill(str(value))
    page.wait_for_timeout(600)  # searchable menus load asynchronously
    options = page.locator("[role=option]")
    texts = [re.sub(r"\s+", " ", options.nth(i).inner_text(timeout=2000)).strip() for i in range(min(options.count(), 300))]
    chosen = _choose(texts, value)
    if chosen is None:
        box.fill("")
        page.keyboard.press("Escape")
        raise ValueError("no option matches the verified value")
    options.nth(texts.index(chosen)).click()


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
                wanted = re.sub(r"\s+", " ", str(r.value)).strip()
                target = next((group.nth(i) for i in range(group.count()) if _option_text(page, group.nth(i)) == wanted), None)
                if target is None:
                    raise ValueError("option not found")
                target.check()
            elif r.kind == "checkbox":
                locator.first.set_checked(bool(r.value))
            elif r.kind == "combobox":
                _pick_combobox(page, locator.first, r.value)
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
    # e.g. Greenhouse's emailed security code, or a challenge shown on submit.
    late = sorted({b.kind for b in detect_blockers(page, adapter) if b.kind in ("captcha", "mfa", "login")})
    return "unknown", {"url": page.url, "errors": errors, "post_submit_blockers": late,
                       "note": "No confirmation message or confirmation URL observed."}


def inspect_form(page, adapter, url) -> dict:
    """Read-only check of a live candidate page for adapter validation.

    Reports blockers, the form root, every discovered question with its
    classification, and whether a submit button is found. Nothing is typed,
    uploaded, selected or submitted.
    """
    from .forms import classify
    response = page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    blockers = detect_blockers(page, adapter, response.status if response else None)
    root = _form_root(page, adapter, open_form=False)
    fields = discover(page, root) if root else []
    submit = next((s for s in adapter.submit_selectors if root and page.locator(root + " " + s).count()), None)
    return {"url": url, "final_url": page.url, "status": response.status if response else None,
            "blockers": [{"kind": b.kind, "detail": b.detail} for b in blockers], "form": root, "submit_button": submit,
            "fields": [{"label": f.label, "kind": f.kind, "required": f.required, "concept": classify(f), "options": f.options[:12]} for f in fields]}


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
        if all(b.kind == "captcha" for b in report.blockers):
            # Reference only, for the human who completes it: list each question and
            # whether a verified answer exists. Nothing is typed, uploaded or clicked.
            root = _form_root(page, adapter, open_form=False)
            if root:
                report.fields = discover(page, root)
                report.resolutions = resolve_fn(report.fields)
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
