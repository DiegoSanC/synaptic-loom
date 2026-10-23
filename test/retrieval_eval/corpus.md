<!--
The retrieval benchmark's logbook: one MADE-UP project (an invoicing service, "ledger"),
written the way summarizer v4 writes entries (prompts/summarizer_v4.md). It is synthetic
on purpose: no real project, person or deployment appears here (AGENTS.md rule 15).

Each entry opens with a marker line: `<!-- entry <key> <YYYY-MM-DD> -->`. The key names
the entry in queries.json (`e07.2` = entry e07's second item, in document order); the date
becomes the entry's `created_at` and its frontmatter timestamp. Some later entries restate
an earlier fact with a new value and no supersession link between them, which is what the
`stale` queries measure. They come in the two shapes a logbook produces: the change gets
an entry of its own (e15, e18, e24, e26), or it is made in passing inside a longer session
about something else (e27, e28) while the original had a short entry to itself. BM25's
length normalization favours the short entry, so the two shapes pull ranking in opposite
directions. Add entries at the end, so existing keys keep their meaning.
-->

<!-- entry e01 2025-11-04 -->
## Context
Set up the HTTP layer of the ledger service and agreed on the error format.
## Decisions
- [decision] **FastAPI instead of Flask for the HTTP layer** — typed request models and async handlers; Flask was tried for a day and dropped because every handler needed its own validation. Evidence: `app/main.py`.
- [method] **Every endpoint answers errors as JSON with `error` and `detail` keys** — clients parse a single shape. Evidence: `app/errors.py`.
- [task] **Write request examples for the invoice endpoints in the OpenAPI schema** — no owner stated. Evidence: "add the examples before the beta".

<!-- entry e02 2025-11-12 -->
## Context
Chose the database for the ledger and how its schema is upgraded.
## Decisions
- [decision] **PostgreSQL instead of SQLite for the ledger store** — the billing workers write concurrently and SQLite allows one writer at a time. Evidence: `docker-compose.yml`.
- [decision] **Schema migrations run at startup with `alembic upgrade head`** — keeps a deploy to one step; running them from CI was rejected because CI has no network path to the database. Evidence: `app/startup.py`.
- [fact] **Each billing worker holds its own connection pool, capped at 20 connections** — (rationale not stated). Evidence: `app/db.py`.

<!-- entry e03 2025-11-20 -->
## Context
First version of API authentication.
## Decisions
- [decision] **Bearer tokens instead of session cookies for the API** — the clients are scripts and workers, not browsers. Evidence: `app/auth.py`.
- [fact] **API tokens expire after 30 days and are rotated by hand** — (rationale not stated). Evidence: `app/auth.py`.
- [method] **Tokens are stored only as SHA-256 hashes** — a leaked database must not leak usable tokens. Evidence: `app/auth.py`.

<!-- entry e04 2025-12-02 -->
## Context
Pricing of the paid plan and how money amounts are represented.
## Decisions
- [fact] **The Pro plan price is 49 EUR per seat per month** — set by the founders. Evidence: `pricing.yaml`.
- [decision] **Money amounts are stored as integer cents, never as floats** — float rounding produced invoice totals that were off by a cent; Python's Decimal was considered and dropped because the amounts travel as JSON. Evidence: `app/money.py`.

<!-- entry e05 2025-12-10 -->
## Context
Moved continuous integration to a hosted service and sped up the pipeline.
## Decisions
- [decision] **GitHub Actions instead of a self-hosted Jenkins** — nobody wanted to maintain the Jenkins machine. Evidence: `.github/workflows/ci.yml`.
- [method] **Cache pip downloads in CI, keyed on `uv.lock`** — cut the pipeline from 9 to 4 minutes. Evidence: `.github/workflows/ci.yml`.
- [fact] **The integration tests need a running Docker daemon** — they start PostgreSQL in a container. Evidence: `tests/conftest.py`.

<!-- entry e06 2025-12-18 -->
## Context
Fixed a CI failure after the virtualenv was moved.
## Decisions
- [fact] **CI failed because pytest was not installed in the interpreter the job used** — after `.venv` moved, the job fell back to the system Python; calling `.venv/bin/python -m pytest` fixed it. Evidence: `.github/workflows/ci.yml`.
- [method] **CI invokes every tool through `.venv/bin/python -m`, never through PATH** — the PATH lookup is what picked the wrong interpreter. Evidence: "never rely on PATH in CI".

<!-- entry e07 2026-01-08 -->
## Context
Invoice PDF generation.
## Decisions
- [decision] **WeasyPrint for invoice PDFs instead of wkhtmltopdf** — wkhtmltopdf is no longer maintained. Evidence: `app/pdf.py`.
- [fact] **Rendering one invoice PDF takes about 800 ms, so it runs in a background worker, not in the request** — rendering inside the request hit the proxy's 30-second limit on batch exports. Evidence: `app/pdf.py`.
- [task] **Embed the company logo as SVG in the invoice template** — no owner stated. Evidence: `templates/invoice.html`.

<!-- entry e08 2026-01-15 -->
## Context
Caching of the currency exchange rates used on invoices.
## Decisions
- [decision] **Redis for the exchange-rate cache instead of an in-process dictionary** — several workers must see the same rates. Evidence: `app/rates.py`.
- [fact] **Exchange rates are cached for one hour** — the rate provider publishes hourly. Evidence: `app/rates.py`.

<!-- entry e09 2026-01-22 -->
## Context
Delivery of webhooks to customer endpoints.
## Decisions
- [decision] **Webhook deliveries retry with exponential backoff, five attempts, then land in a dead-letter table** — a fixed one-minute interval hammered slow receivers. Evidence: `app/webhooks.py`.
- [fact] **A receiver must answer within 10 seconds or the delivery counts as failed** — (rationale not stated). Evidence: `app/webhooks.py`.

<!-- entry e10 2026-02-03 -->
## Context
First production deployment.
## Decisions
- [decision] **Deploy to Fly.io with one machine per region** — Heroku was dropped over price. Evidence: `fly.toml`.
- [method] **Never run `fly deploy` from a laptop; only the release workflow deploys** — two people shipped different builds on the same day. Evidence: `.github/workflows/release.yml`.
- [fact] **The health check path is `/healthz`** — (rationale not stated). Evidence: `fly.toml`.

<!-- entry e11 2026-02-11 -->
## Context
Investigated why the month-end billing run died.
## Decisions
- [fact] **The month-end billing run exhausted PostgreSQL's connection limit** — every worker opened its own pool; fixed by sharing one pool per process and putting PgBouncer in front of the database. Evidence: `app/db.py`, "the run died at 02:14".
- [decision] **PgBouncer in transaction mode in front of PostgreSQL** — session mode kept too many server connections open. Evidence: `deploy/pgbouncer.ini`.

<!-- entry e12 2026-02-20 -->
## Context
Built the internal admin pages.
## Decisions
- [decision] **Server-rendered Jinja templates for the admin pages instead of a React single-page app** — two developers and no frontend specialist. Evidence: `app/templates/`.
- [method] **No inline scripts in templates: the Content-Security-Policy forbids them** — (rationale not stated). Evidence: `app/security.py`.

<!-- entry e13 2026-03-03 -->
## Context
VAT handling on invoices.
## Decisions
- [fact] **EU business-to-consumer invoices charge VAT at the customer's country rate** — (rationale not stated). Evidence: `app/tax.py`.
- [decision] **Tax rates come from a versioned YAML file instead of an external API** — an invoice must be reproducible years later. Evidence: `tax_rates.yaml`.
- [task] **Add Estonia's 2026 VAT rate change** — deadline: before July. Evidence: "Estonia changes in July".

<!-- entry e14 2026-03-17 -->
## Context
Logging format and what may be logged.
## Decisions
- [decision] **Structured JSON logs via structlog instead of plain-text lines** — the log search needs fields to filter on. Evidence: `app/logging.py`.
- [method] **Never log full card numbers or tokens; log only the last four characters** — (rationale not stated). Evidence: `app/logging.py`.

<!-- entry e15 2026-04-02 -->
## Context
Second version of API authentication, for unattended workers.
## Decisions
- [decision] **Device tokens expire after 90 days and renew automatically** — the 30-day manual rotation locked unattended workers out twice; a token renews once a quarter of its life remains. Evidence: `app/auth.py`.
- [fact] **Revoking a device revokes every token it renewed into** — so a stolen old token cannot outlive the revocation. Evidence: `app/auth.py`.

<!-- entry e16 2026-04-15 -->
## Context
How refunds are recorded.
## Decisions
- [decision] **Refunds are new negative invoices, never edits of the original** — an issued invoice is immutable for the audit trail. Evidence: `app/refunds.py`.
- [method] **An issued invoice is never modified; every correction goes through a credit note** — the accountant requires it. Evidence: "legal requires it".

<!-- entry e17 2026-05-06 -->
## Context
Monitoring and alerting.
## Decisions
- [decision] **Sentry for exceptions and Grafana Cloud for metrics** — a self-hosted Prometheus was dropped for its maintenance cost. Evidence: `app/observability.py`.
- [fact] **Alerts page the on-call only for the payment and webhook queues** — every other alert goes to the team channel. Evidence: `alerts.yaml`.

<!-- entry e18 2026-06-01 -->
## Context
Price change for the paid plan.
## Decisions
- [fact] **The Pro plan price rises to 79 EUR per seat per month from July** — raised after the cost review; existing customers keep 49 EUR until their renewal. Evidence: `pricing.yaml`.
- [task] **Email existing customers about the new price** — deadline: June 15. Evidence: "send the notice two weeks ahead".

<!-- entry e19 2026-06-20 -->
## Context
Fixed invoice PDFs failing in production.
## Decisions
- [fact] **Invoice PDF rendering crashed in the slim Docker image because the Pango system library was missing** — installing `libpango-1.0-0` in the `Dockerfile` fixed it. Evidence: `Dockerfile`.
- [method] **The CI image and the production image are built from the same `Dockerfile`** — the crash only showed up in production. Evidence: `.github/workflows/ci.yml`.

<!-- entry e20 2026-07-08 -->
## Context
Background job framework.
## Decisions
- [decision] **Dramatiq with Redis for background jobs instead of Celery** — simpler retries and no separate scheduler process. Evidence: `app/jobs.py`.
- [fact] **The month-end billing run is one job per customer instead of one big job** — one failing customer no longer restarts the whole run. Evidence: `app/billing.py`.

<!-- entry e21 2026-07-22 -->
## Context
Two-factor login for the admin pages.
## Decisions
- [decision] **TOTP two-factor login for the admin pages; SMS codes were rejected** — SIM-swap attacks. Evidence: `app/admin_auth.py`.
- [task] **Recovery codes for admins who lose their phone** — no owner stated. Evidence: "we still need a recovery path".

<!-- entry e22 2026-08-05 -->
## Context
Database backups.
## Decisions
- [decision] **Nightly `pg_dump` backups to object storage, kept for 30 days** — the provider's disk snapshots could not be restored into another region. Evidence: `deploy/backup.sh`.
- [method] **Restore the latest backup into a scratch database every month** — an untested backup is not a backup. Evidence: "monthly restore drill".

<!-- entry e23 2026-08-19 -->
## Context
API rate limiting.
## Decisions
- [decision] **Rate limiting per API token at 100 requests per minute** — per-IP limiting punished customers behind one NAT. Evidence: `app/ratelimit.py`.
- [fact] **A rate-limited request gets a 429 response with a Retry-After header** — (rationale not stated). Evidence: `app/ratelimit.py`.

<!-- entry e24 2026-09-02 -->
## Context
Fresher exchange rates for large invoices.
## Decisions
- [fact] **Exchange rates are now cached for 15 minutes** — finance asked for fresher rates on large invoices, and the provider plan was upgraded. Evidence: `app/rates.py`.

<!-- entry e25 2026-09-16 -->
## Context
Data retention after an account is closed.
## Decisions
- [decision] **Customer personal data is deleted 90 days after the account closes; invoices are kept for 10 years with their personal fields anonymised** — tax law requires the invoices and the GDPR requires the deletion. Evidence: `app/retention.py`.

<!-- entry e26 2026-10-01 -->
## Context
Simplified the production topology.
## Decisions
- [decision] **Run two Fly machines in the primary region only; the second region was dropped** — cross-region writes to PostgreSQL added 80 ms per request. Evidence: `fly.toml`.

<!-- entry e27 2026-10-08 -->
## Context
Review of the October payment-queue incident: what failed, what changes, and what is still open.
## Decisions
- [fact] **The payment queue stalled for 40 minutes because one slow webhook receiver held every worker** — deliveries to the same receiver were not limited, so its repeated attempts filled the queue. Evidence: "the queue was stuck behind one customer".
- [decision] **At most two concurrent deliveries per receiver** — a per-receiver limit was chosen over a global one, so one slow customer cannot block the others. Evidence: `app/webhooks.py`.
- [decision] **A webhook receiver now gets 30 seconds to answer before the delivery counts as failed** — large customers' endpoints regularly needed more than the old limit. Evidence: `app/webhooks.py`.
- [task] **Write the incident post-mortem for the affected customers** — owner: that week's on-call. Evidence: "we owe them a write-up".

<!-- entry e28 2026-10-15 -->
## Context
Compliance review before the first enterprise contract: retention, access and audit.
## Decisions
- [decision] **Backups are now kept for 90 days instead of 30** — the enterprise contract requires three months of history; the extra storage was judged cheap. Evidence: `deploy/backup.sh`.
- [method] **Production database access goes through the bastion host with personal accounts only** — shared credentials were removed. Evidence: "no more shared accounts on the bastion".
- [fact] **The audit log records every invoice view by support staff** — the contract asks who saw a customer's invoices. Evidence: `app/audit.py`.
- [task] **Ask the auditors whether anonymised invoices still count as personal data** — no owner stated. Evidence: "open question for the auditors".
