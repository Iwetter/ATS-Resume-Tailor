# Architectural references

Inspected relevant individual files through GitHub, without cloning repositories. The implementation here is original; no reference code, styles or assets were copied or vendored. In particular, no AGPL code from OpenResume is incorporated.

- [srbhr/Resume-Matcher](https://github.com/srbhr/Resume-Matcher), commit `ff5b370372d39b6cb6b56fd0dad88a2fe5c3abce`: `apps/backend/app/services/ats_parse/extract.py`, `ats_parse/roundtrip.py` and `tailor_selection.py`. Observations: preserve document order and layout warnings; verify structured fields after PDF extraction; report page-fit failures without hiding selected content. This build replaces LLM selection/scoring with deterministic local ranking. License inspected: Apache-2.0.
- [xitanggg/open-resume](https://github.com/xitanggg/open-resume), commit `4f8255a2c763479837f69f1dccf2a3338730cd79`: `src/app/lib/parse-resume-from-pdf/group-lines-into-sections.ts` and `src/app/components/Resume/ResumePDF/styles.ts`. Observations: section grouping can use recognizable headings and document structure; typography/spacing should be consistent. Independent Python/ReportLab implementation, no React dependency. License inspected: AGPL-3.0.
- [akshatarameshdhande/ATS-RESUME-MATCHER](https://github.com/akshatarameshdhande/ATS-RESUME-MATCHER), commit `bc920c2bce6e333eff8005a1341878682e546fed`: `app.py`, `utils/pdf_parser.py`, `utils/skill_extractor.py`, `utils/matcher.py`. Observations: small Streamlit module boundaries, TF-IDF matching, and punctuation-aware technical tokens. This app separates readability from alignment and traces claims to specific evidence. License inspected: MIT.
- [hemgandhi13/ats_resume_analyzer](https://github.com/hemgandhi13/ats_resume_analyzer), commit `30920af1818c87a7ff4bc837cfba0fa1ed8fc7e2`: `src/ats/parse.py` and `src/ats/app.py`. Observations: contact/section parsing, explicit Analyze action and content-hash session caches. No LICENSE file was present in the repository tree inspected; no code was reused. Automatic LLM scoring, default AI enablement and file-based resume logs were not adopted.

## Real test posting

[Western Digital Summer 2027 Intern Hardware Engineering](https://jobs.smartrecruiters.com/WesternDigital/744000140949875-summer-2027-intern-hardware-engineering), retrieved October 20, 2026 through the browsing tool. `tests/fixtures/western_digital_job.txt` preserves the real title, Job Description and Qualifications sections, including all listed engineering tracks. Company promotion, EEO and compensation text are excluded from the fixture. This is a saved snapshot, not a guarantee the live job remains open. No substitute requirements were fabricated. Direct Python requests timed out on this machine; URL failures are also tested with mocked transport.

## Optional OpenAI implementation

The request format follows [official OpenAI Structured Outputs documentation](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat): Chat Completions `response_format` with JSON Schema, `strict: true`, all object properties required and `additionalProperties: false`. The implementation handles refusal/incomplete responses as failures and does not automatically retry. The API adapter is tested with mocked HTTP; no live model or billing was used.

PyMuPDF and ReportLab have their own licenses distributed with their installed packages. Review dependency licensing before redistributing this application beyond local personal use.
