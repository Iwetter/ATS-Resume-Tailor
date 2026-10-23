"""Greenhouse/Lever adapter fixes found by read-only inspection of live pages
(job-boards.greenhouse.io and jobs.lever.co, October 2026). The fixtures copy
the live DOM patterns: Greenhouse's "Attach" upload groups, React-Select
comboboxes with hidden required proxies, and Lever's custom-question cards."""

import pytest

from conftest import AAEnv, aa_profile
from autoapply import db as S
from autoapply.browser import BrowserFactory, GreenhouseAdapter, LeverAdapter, inspect_form, run_application
from autoapply.forms import resolve
from autoapply.selftest import FixtureRouter, GH_BASE, LEVER_BASE, greenhouse_form

pytestmark = pytest.mark.usefixtures("chromium_available")

COMBO_JS = """<script>
document.querySelectorAll('[data-combo]').forEach(input => {
  const list = document.getElementById(input.id + '-listbox');
  const all = JSON.parse(input.dataset.combo);
  const searchable = input.dataset.searchable === '1';
  const render = () => {
    const q = input.value.toLowerCase();
    const items = searchable && !q ? [] : all.filter(o => o.toLowerCase().includes(q));
    list.innerHTML = items.map((o, i) => `<div role="option" id="${input.id}-opt-${i}">${o}</div>`).join('') || '<div class="empty">No options</div>';
    list.hidden = false; input.setAttribute('aria-expanded', 'true'); input.setAttribute('aria-controls', list.id);
  };
  const hide = () => { list.hidden = true; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-controls'); };
  input.addEventListener('focus', render); input.addEventListener('input', render);
  input.addEventListener('keydown', e => { if (e.key === 'ArrowDown') render(); if (e.key === 'Escape') hide(); });
  list.addEventListener('mousedown', e => { if (e.target.getAttribute('role') === 'option') { input.value = e.target.textContent; input.nextElementSibling.value = 'x'; hide(); } });
});
</script>"""


def combo(fid, label, options, searchable=False):
    import html
    import json
    return (f'<div class="select"><label id="{fid}-label" for="{fid}" class="label select__label">{label}<span aria-hidden="true">*</span></label>'
            f'<div class="select-shell"><input id="{fid}" class="select__input" role="combobox" aria-labelledby="{fid}-label" aria-required="true" '
            f'autocomplete="off" data-searchable="{1 if searchable else 0}" data-combo="{html.escape(json.dumps(options))}">'
            '<input required tabindex="-1" aria-hidden="true" class="requiredInput" style="opacity:0;width:1px;height:1px" value="">'
            f'<div id="{fid}-listbox" role="listbox" hidden></div></div></div>')


GH_NEW_BOARD = f"""<!doctype html><html><head><meta charset="utf-8"><title>Fixture</title></head><body>
<form id="application-form" class="application--form" action="/fixtureco/jobs/101/applications" method="post">
<div class="field"><label for="first_name">First Name<span>*</span></label><input id="first_name" aria-required="true"></div>
<div class="field"><label for="last_name">Last Name<span>*</span></label><input id="last_name" aria-required="true"></div>
<div class="field"><label for="email">Email<span>*</span></label><input id="email" aria-required="true"></div>
<div role="group" aria-labelledby="upload-label-resume" aria-required="true" class="file-upload"><div id="upload-label-resume" class="label upload-label">Resume/CV<span class="required">*</span></div>
<div class="button-container"><div><button type="button" class="btn">Attach</button><label class="visually-hidden" for="resume">Attach</label>
<input id="resume" class="visually-hidden" type="file" accept=".pdf" style="position:absolute;width:1px;height:1px;opacity:0"></div></div></div>
{combo("degree--0", "Degree", ["Associate's Degree", "Bachelor's Degree", "Master's Degree"])}
{combo("school--0", "School", ["Example Institute of Technology", "Example State University", "Other College"], searchable=True)}
{combo("question_1", "Will you now or in the future require visa sponsorship?", ["Yes", "No"])}
<button type="submit" class="btn btn--pill">Submit application</button></form>{COMBO_JS}</body></html>"""

LEVER_CARDS = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<form id="application-form" action="/fixtureco/0a1b2c3d-fixture/apply" method="post"><ul>
<li class="application-question resume"><label><div class="application-label">Resume/CV <span class="required">✱</span></div>
<div class="application-field"><a href="#">ATTACH RESUME/CV</a><input type="file" name="resume" style="opacity:0;position:absolute"></div></label></li>
<li class="application-question"><label><div class="application-label">Full name<span class="required">✱</span></div><div class="application-field"><input type="text" name="name" required></div></label></li>
<li class="application-question custom-question"><div><div class="application-label full-width"><div class="text">Will you now or in the future require sponsorship for employment visa status?<span class="required">✱</span></div></div>
<div class="application-field full-width required-field"><ul data-qa="multiple-choice">
<li><label><input type="radio" name="cards[abc][field0]" value="Yes"><span class="application-answer-alternative">Yes</span></label></li>
<li><label><input type="radio" name="cards[abc][field0]" value="No"><span class="application-answer-alternative">No</span></label></li></ul></div></div></li>
<li class="application-question custom-question"><div><div class="application-label full-width"><div class="text">Please include your intended graduation year<span class="required">✱</span></div></div>
<div class="application-field full-width required-field"><select name="cards[def][field0]" required><option value="">Select...</option><option>2027</option><option>2028</option></select></div></div></li>
<li class="application-question custom-question"><div><div class="application-label full-width text"><div class="text">Name Pronunciation | How do you pronounce your name?</div></div>
<div class="application-field full-width"><input class="card-field-input" type="text" placeholder="Type your response" name="cards[def][field1]"></div></div></li>
</ul><button id="btn-submit" type="button">Submit application</button></form></body></html>"""


@pytest.fixture
def settings(tmp_path, master):
    return AAEnv(tmp_path, master).settings


def test_greenhouse_new_board_fields_are_discovered_correctly(settings, tmp_path):
    router = FixtureRouter({GH_BASE: GH_NEW_BOARD})
    with BrowserFactory(settings, router).page() as page:
        report = inspect_form(page, GreenhouseAdapter(), GH_BASE)
    fields = {f["label"]: f for f in report["fields"]}
    assert set(fields) == {"First Name*", "Last Name*", "Email*", "Resume/CV*", "Degree*", "School*", "Will you now or in the future require visa sponsorship?*"}
    assert fields["Resume/CV*"]["concept"] == "resume" and fields["Resume/CV*"]["required"]  # not "Attach"
    assert fields["Degree*"]["options"] == ["Associate's Degree", "Bachelor's Degree", "Master's Degree"]
    assert fields["School*"]["options"] == []  # searchable: nothing listed until typed
    assert report["submit_button"] and router.posts == [] and router.blocked == []


def test_greenhouse_comboboxes_are_filled_with_exact_options(settings, tmp_path):
    router = FixtureRouter({GH_BASE: GH_NEW_BOARD})
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fixture")
    bank = [{"id": "a1", "question": "Degree", "answer": "Bachelor's Degree", "verified_at": "2026-10-23"}]
    profile = aa_profile()  # degree "Bachelor of Science" does not match the employer's list by itself
    with BrowserFactory(settings, router).page() as page:
        report = run_application(page, GreenhouseAdapter(), GH_BASE, lambda fields: resolve(fields, profile, bank, resume_path=str(resume)), tmp_path / "ev")
        values = page.evaluate("() => ({degree: document.getElementById('degree--0').value, school: document.getElementById('school--0').value, "
                               "sponsor: document.getElementById('question_1').value, files: document.getElementById('resume').files.length})")
    by_label = {r.label: r for r in report.resolutions}
    assert by_label["Degree*"].source == "answer_bank" and by_label["Resume/CV*"].source == "resume"
    assert report.fill_errors == [] and not report.submitted
    assert values == {"degree": "Bachelor's Degree", "school": "Example Institute of Technology", "sponsor": "No", "files": 1}
    assert router.posts == []


def test_combobox_without_a_matching_option_is_an_error_not_a_guess(settings, tmp_path):
    router = FixtureRouter({GH_BASE: GH_NEW_BOARD})
    profile = aa_profile(school="Nowhere Polytechnic")
    with BrowserFactory(settings, router).page() as page:
        report = run_application(page, GreenhouseAdapter(), GH_BASE, lambda fields: resolve(fields, profile, [], resume_path=None), tmp_path / "ev")
        school = page.evaluate("() => document.getElementById('school--0').value")
    assert any(e.startswith("School*") for e in report.fill_errors) and school == ""


def test_lever_custom_question_cards(settings, tmp_path):
    router = FixtureRouter({LEVER_BASE: LEVER_CARDS})
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fixture")
    with BrowserFactory(settings, router).page() as page:
        report = run_application(page, LeverAdapter(), LEVER_BASE, lambda fields: resolve(fields, aa_profile(), [], resume_path=str(resume)), tmp_path / "ev")
        checked = page.evaluate("() => document.querySelector('input[name=\"cards[abc][field0]\"]:checked').value")
        year = page.evaluate("() => document.querySelector('select').value")
    labels = {r.label: r for r in report.resolutions}
    assert labels["Resume/CV ✱"].required and labels["Resume/CV ✱"].source == "resume"
    sponsor = next(r for r in report.resolutions if r.label.startswith("Will you now or in the future require sponsorship"))
    assert sponsor.required and sponsor.value == "No" and checked == "No"
    grad = next(r for r in report.resolutions if r.label.startswith("Please include your intended graduation year"))
    assert grad.concept == "graduation_year" and year == "2028"
    assert any(r.label.startswith("Name Pronunciation") and not r.required for r in report.resolutions)
    assert report.fill_errors == [] and router.posts == []


def test_captcha_page_lists_questions_for_the_human_without_typing_anything(settings, tmp_path):
    router = FixtureRouter({GH_BASE: greenhouse_form(captcha=True)})
    with BrowserFactory(settings, router).page() as page:
        report = run_application(page, GreenhouseAdapter(), GH_BASE, lambda fields: resolve(fields, aa_profile(), [], resume_path="r.pdf"), tmp_path / "ev",
                                 before_submit=lambda r: True)
        typed = page.evaluate("() => document.getElementById('first_name').value")
    assert [b.kind for b in report.blockers] == ["captcha"] and not report.submitted
    assert {r.label: r.source for r in report.resolutions}["First Name *"] == "profile"
    assert typed == "" and report.filled == [] and router.posts == []


class CodeAfterSubmit(FixtureRouter):
    def __call__(self, route):
        if route.request.method == "POST":
            self.posts.append({"url": route.request.url})
            return route.fulfill(status=200, content_type="text/html", body="<html><body><p>Enter the verification code we sent to your email.</p>"
                                                                            "<input autocomplete='one-time-code'></body></html>")
        return FixtureRouter.__call__(self, route)


def test_code_requested_after_submit_is_reported_as_unknown(tmp_path, master):
    from test_autoapply_browser import prepared
    env = AAEnv(tmp_path, master, router=CodeAfterSubmit(), allow_real_submission=True)
    app = prepared(env)
    env.engine.approve(app["id"], app["approval_hash"])
    env.engine.process()
    app = env.auto.application(app["id"])
    assert app["state"] == S.UNKNOWN and "asked for more after the click" in app["reasons"][0]
    assert "mfa" in app["confirmation"]["post_submit_blockers"] or "login" in app["confirmation"]["post_submit_blockers"]
    env.engine.process()
    assert len(env.router.posts) == 1  # never retried
