# Private application history (before enabling submissions)

The app cannot infer all previously submitted applications from GitHub or Gmail.
Import confirmed applications manually into `data/application_history.csv`
(the `data/` folder is git-ignored). Do not commit this file.

CSV format:

```csv
company,title,status,source_id
Example Research Institute,Robotics Student Engineer,applied,R123
Example Electronics,Embedded Systems Intern,unknown,R456
```

Statuses: `applied`, `submitted`, `interview`, `rejected`, `withdrawn`,
`offer`, `unknown`, `pending confirmation`.

Supply the employer requisition ID in optional `source_id` when known. Distinct
requisitions count separately even if their titles match. One confirmed role counts once toward the per-company lifetime limit even
if multiple confirmation emails exist. Unknown outcomes block reapplying to
the same title; confirm the employer portal before changing them. Enter a
company with an empty title when the specific role is genuinely unknown:
this consumes one lifetime slot but cannot identify a title duplicate.

Default `AUTOAPPLY_MAX_PER_COMPANY_TOTAL=1` is deliberately conservative.
Do not blindly increase it. Verified company-name aliases and legal suffix
normalization apply to application history and bot submission counts, but
some mergers, brands and ATS board names still require manual review.

**Important limitations:** This feature does not connect Gmail or read a
personal spreadsheet automatically. The CSV must be populated from a verified
history, which may include application records from other email accounts or
employer dashboards. `AUTOAPPLY_ALLOW_REAL_SUBMISSION=false` remains the
default and must stay false until the operator confirms history completeness,
a real profile, official forms, employer permissions, successful dry runs,
and production deployment. Company limits are also checked immediately before
submission inside the same SQLite write transaction as the submission intent.
Concurrent workers must share this exact persistent database (never separate copies).
Expired leases recover on each worker cycle; active leases are left untouched.

Imported CSV records are retained in an append-only `imported_application_history`
table, including first-seen timestamps. Removing a CSV row does not erase a prior
application. Dashboard Applied/Interview/Rejected/Withdrawn statuses and historical
status actions also count. Unknown outcomes consume quota. Records appearing in
both the bot and an external import may conservatively consume extra slots; review
these manually rather than increasing limits. Back up the database and CSV together.
