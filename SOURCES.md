# Internship Hunter source coverage

Coverage checked October 20, 2026 (America/New_York). Machine-readable scan timestamps use UTC; October 21 at 02:24 UTC is October 20 at 10:24 PM Eastern.

## Current watchlist and live result

The expanded watchlist contains **28 verified employer board endpoints**. **23 are enabled** for scanning: Anduril Industries, Astranis, Rocket Lab, Relativity Space, Figure, Nuro, Redwood Materials, Waymo, Verkada, Samsara, Neuralink, IonQ, Epirus, Shield AI, Zoox, Reliable Robotics, SpaceX, Vast, Formlabs, Markforged, NVIDIA, RTX and GE Vernova.

The expanded production scan successfully read **23/23 enabled boards**, retained **103 engineering internship/co-op records**, and found **10 confirmed recent Spring/Summer 2027 U.S. postings** within the 7-day window. The three new Workday boards added 38 records before location, term and eligibility filtering: NVIDIA 2, RTX 29 and GE Vernova 7. Their public sitemaps contained 100, 4,807 and 2,230 URLs respectively. A separate Personio live check read Wingcopter's eight public jobs with zero matching internship titles; it was not added to the U.S. watchlist. The attempted AME Recruitee board was skipped because robots returned a redirect, so its access could not be confirmed. Recruitee is fixture-tested, with no claim of successful live coverage for that employer.

The production scan reused 132 actual public-response cache entries with zero new requests. New Workday entries were warmed by the immediately preceding live adapter checks, preserving their original timestamps. The earlier Phase 2 CLI scan had retained 65 records and three confirmed recent target-scope jobs; it is retained as a historical report. Source Last Checked timestamps never refresh merely because of cache hits.

These observations are real public responses, not test fixtures. See `MORE_BOARDS_LIVE_REPORT.json` and `MORE_BOARDS_PRODUCTION_SCAN.json` for new board counts, timestamps, limits and cache statistics; `LIVE_SCAN_REPORT.json` records the earlier scan. The current workspace's SQLite database holds the real records. No verified Hunter master was configured at the scan, so no personalized fit scores were assigned. Availability can change after the recorded check.

Five additional endpoints returned public job data during investigation but are **disabled by default** because the actual scanner's robots checks prevented access:

| Employers | Adapter | Restriction observed |
| --- | --- | --- |
| Saronic, Standard Bots, Crusoe | Ashby | `api.ashbyhq.com/robots.txt` returned HTTP 401, so permission could not be confirmed; scanner skipped the host |
| Western Digital, Bosch Group | SmartRecruiters | The API host's robots rules disallowed the posting paths; scanner skipped them |

Their adapters are implemented and tested with saved API-shaped fixtures. Enabling a board in the UI retries the same permission checks; it does not bypass robots, authentication or anti-bot controls. A public API existing is not treated as permission to ignore a restriction. The broader initial probe is retained in `LIVE_SOURCE_REPORT.json` as an investigation log; its Greenhouse `regent` tenant was excluded from the final watchlist after identity review because it belonged to a finance employer. IonQ was verified against its official careers page and replaced it. Reliable Robotics' official Lever board was confirmed by its company-homepage link; it currently returned zero jobs, which is not evidence of an opening.

## Adapter contracts and dates

| Adapter | Public GET interface | Publication evidence |
| --- | --- | --- |
| Greenhouse | List published jobs, then bounded relevant-job details | `first_published`; `updated_at` is displayed separately |
| Lever | Paginated public postings with full available descriptions | Posting date unknown; `createdAt` is never assumed to mean published; `updatedAt` is kept separately if present |
| Ashby | Public job-board API with optional explicit compensation | `publishedAt`, labeled **last published**; this may be a republication, not the original first posting |
| SmartRecruiters | Paginated public company postings and relevant detail records | `releasedDate` |
| Workday | Public `siteMap.xml`, bounded same-employer sitemap indexes, then relevant public HTML pages with unique JobPosting JSON-LD | Explicit `datePosted`; sitemap `lastmod` is never publication evidence. Coverage is always treated as partial for closure detection. |
| Personio | Employer-enabled public `/xml?language=en` feed | Posting date unknown; `createdAt` is never publication evidence |
| Recruitee | Public `/api/offers.xml` feed, published records only | Only explicit `published_at` if supplied; creation, update and feed-add dates never imply first publication |

Official implementation references: [Greenhouse Job Board API](https://docs.greenhouse.io/job-board), [Lever Postings API](https://github.com/lever/postings-api), [Ashby public job-posting API](https://developers.ashbyhq.com/docs/public-job-posting-api), [SmartRecruiters endpoints](https://developers.smartrecruiters.com/docs/endpoints), and [SmartRecruiters object definitions](https://developers.smartrecruiters.com/docs/objects). Employer identity checks included [IonQ careers](https://www.ionq.com/careers) and [Reliable Robotics' hosted board](https://jobs.lever.co/reliable?location=Remote).

Official references for the extension: [Personio XML integration](https://support.personio.de/hc/en-us/articles/207576365-Integrate-jobs-from-Personio-into-your-website-via-XML), [Recruitee public XML export](https://support.recruitee.com/en/articles/8206006-faq-data-export), [Recruitee feed semantics](https://docs.recruitee.com/docs/feed). Workday evidence came directly from its public employer robots files, sitemaps and HTML JobPosting records, including [NVIDIA's sitemap](https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/siteMap.xml), [RTX's sitemap](https://globalhr.wd5.myworkdayjobs.com/REC_RTX_Ext_Gateway/siteMap.xml) and [GE Vernova's sitemap](https://gevernova.wd5.myworkdayjobs.com/Vernova_ExternalSite/siteMap.xml). No general Workday search API is assumed to exist.

## External boards and imports

LinkedIn, Indeed, Handshake, Glassdoor, USAJOBS, Built In, ZipRecruiter and Dice have user-operated search links. Workday employer discovery also has a Google site-search link. Imported descriptions from any board are stored locally with their original URL and optional employer application URL, labeled user-reported publication and availability, and shown in a dedicated view. The app does not fetch imported URLs, log into these sites or synchronize saved jobs. A user confirmation of an employer page is labeled `user-confirmed`, separately from a public-source `open` observation. No imported listing qualifies as an automatically verified recent result. Scoring remains local and tailoring requires a verified master, eligible role and confirmed availability.

[LinkedIn's automation policy](https://www.linkedin.com/help/linkedin/answer/a1340567/automated-activity-on-linkedin?lang=en) restricts automated collection. [Indeed's site rules](https://www.indeed.com/legal?hl=en_US) restrict automated access without permission. This build uses manual search/paste for those boards. [Workable's current careers API instructions](https://help.workable.com/hc/en-us/articles/115012771647-Using-the-Workable-API-to-create-a-careers-page) require employer authentication; no unauthorized widget or private endpoint is used. Its listings can be pasted through the universal import form.

## Behavior and practical limits

- Only supported, allowlisted, unauthenticated public ATS feeds and structured pages are fetched. No CAPTCHA handling, login bypass, hidden internal endpoints, generic search scraping, paid search provider or application POST endpoints are implemented. Redirects are not followed; inability to confirm robots causes the board to be skipped.
- Requests use connect/read timeouts, per-host pacing of at least 0.5 seconds, robots crawl delays, a 24 MB response limit, one retry for transient failures, and a maximum of 60 relevant detail requests per employer. Pagination is bounded at 10,000 rows. Unvisited descriptions take precedence on later scans. Partial descriptions or missing availability confirmations are flagged instead of invented; older saved descriptions are retained if a new detail request fails.
- A successful complete listing is required before missing jobs can be marked closed. Failed or incomplete boards do not close absent records. Cross-source duplicates retain provider observations so one source's disappearance does not close a job still seen on another. Source availability and your application status are separate.
- Deduplication uses ATS identifiers and canonical URLs, then a company/title/location check for cross-provider duplicates. Distinct requisitions on the same board remain distinct. A rare cross-posting collision may need manual review.
- Unknown publication dates, unknown term/location and future dates are separate from confirmed recent target-scope results. First discovery is immutable across rescans. Unknown academic standing, future enrollment, availability and clearance remain visible flags. Graduation-window and degree/GPA checks are conservative rules and require human review for unusual wording.
- The engine searches the enabled employer watchlist, not every employer in the U.S. Add supported hosted-board URLs in the UI. Workday may expose only part of its openings; no omission ever closes a saved Workday record. Up to five sitemap files and 60 relevant descriptions are read per Workday employer per scan. Unsupported sites such as SAP support manual bookmarks and pasted imports. No openings are assumed at employers that were not successfully checked.
- Complete available job descriptions are retained locally. Phase 1's existing 45,000-character scoring/input limit remains; unusually long descriptions may need a reviewed relevant excerpt in the Phase 1 workspace.
- No model calls were made during discovery, filtering, ranking, tests or live scans. Optional tailoring uses Phase 1's one-request structured flow only after selection and an intentional click, and persists cache reservations across restarts. Final wording still requires approval.
