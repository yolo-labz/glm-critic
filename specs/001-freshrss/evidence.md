# Acceptance evidence — 19/09/2026, 15:32 BRT

## Baseline (read-only production inspection)

- No FreshRSS app exists on Dokku. Miniflux has 45 feeds and reports no current parsing errors.
- Critic runs `0839b7c`, scheduler and notify URL unset. Public probe is 403;
  app domains are empty but Dokku proxy is still enabled. This is NOT proof of
  complete ingress removal. No shared nginx/Cloudflare configuration changed.
- `nginx -t`: successful. Eight public neighboring applications: HTTP 200.
- n8n 2.25.7 workflow is active, registered at the wrong prefixed path because
  webhookId is missing. No webhook/email was replayed in this inspection.
- Prior main CI was red: all three Python jobs failed lint (unsorted imports),
  despite the 40 unit tests passing. Fixed without changing CI or disabling lint.

## Isolated desktop acceptance

- Official FreshRSS 1.30.0 digest in `deploy/compose.yaml` pulled and started.
  UI bound to `127.0.0.1:18787` only. Dedicated project `rss-recovery-001`.
- 47/47 subscriptions imported; exported OPML has 47. One permanent redirect:
  NixOS announcements `/c/announcements.rss` → `/c/announcements/8.rss`.
- First refresh: 45 successful sources, **2,430 articles**. Two errors retained:
  Brendan Gregg (empty/incomplete feed response after timeout), MachineLearning
  subreddit (HTTP 429). LocalLLaMA succeeded here; this does not prove it will
  work from the Dokku IP. No rate-limit bypass or authentication attempted.
- `deploy/check_reader.py --mutate-staging`: 47 feeds, 70 unread entries fetched
  through pagination, star twice passed, original star state restored.
- Actual GLM-5.3 dry-run on three public reader entries: **3 judged, 2 signals,
  1,093 input / 2,972 output tokens, no failures**. No verdict log written and
  no reader star changed. Receipt: `live-check.json`.
- First live-model attempt failed locally before TLS: uv Python had no CA file.
  Repeated with `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`, keeping TLS
  verification enabled. This was not a provider/model failure.
- Built critic image and started without published ports. Container checks:
  health **200**, unauthenticated run **401**, authenticated limit=0 **400**.
  `docker inspect`: host port bindings `{}`. Dummy judge key was used for these
  transport-only checks; no paid call from the container.
- `ruff check .`: pass. `python -m unittest discover -s tests -q`: **55 passed**.
- n8n counterpart: **26 tests passed**, including JS renderer injection tests
  and static webhook authentication/path contract; no SMTP invocation.

## Corrected false contract

Miniflux 2.3.3 `toggleStarredHandler` never parses the request body; it calls
`ToggleStarred`. The old test asserting `{"bookmark":false}` only checked an
invented request. Its replacement models the actual server state and proves
GET → PUT toggle → GET verification, followed by a no-op repeat.

Source: https://github.com/miniflux/v2/blob/2.3.3/internal/api/entry_handlers.go

## Research sources and scope

- FreshRSS release: https://github.com/FreshRSS/FreshRSS/releases/tag/1.30.0
- Official image/CLI: https://github.com/FreshRSS/FreshRSS/tree/1.30.0/Docker
- Fever contract: https://freshrss.github.io/FreshRSS/en/developers/06_Fever_API.html
- Extension: https://github.com/FreshRSS/Extensions/tree/main/xExtension-LlmClassification
  returns tags, not the critic verdict schema. It is optional, not a substitute.
- Alternatives: https://tt-rss.org/ and https://www.commafeed.com/;
  existing Miniflux remains the simplest rollback reader. No equal-load reader
  benchmark was run; FreshRSS selection is a feature/operability decision.
- Search tool had one failed response; SearXNG fallback returned results through
  Yep while several other engines were blocked. Canonical upstream sources were
  fetched for the actual API and deployment contracts.

## Not proved / not deployed

No production reader migration, critic deploy, DNS cleanup, n8n activation or
email delivery was performed. Native OpenAI/Anthropic protocols are covered by
contract fixtures, not live funded-provider calls. No accuracy/calibration claim.
Promotion gates and rollback are in `deploy/README.md`.
