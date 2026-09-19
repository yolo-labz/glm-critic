# FreshRSS acceptance and Dokku promotion

## What this deploys

Official FreshRSS **1.30.0**, pinned by digest, with SQLite in a dedicated volume.
Single-user RSS does not need another PostgreSQL service. No custom image or LLM
extension: the existing critic talks to Fever and keeps its own rubric, cache
and log. RSS/Atom sources need no provider-specific adapter. Sites without feeds
can use FreshRSS HTML/XPath or a separately reviewed bridge; not every social
network offers an accessible feed.

`compose.yaml` is an isolated acceptance stack, **not a production deployment**.
Only the reader binds a host port, and only on `127.0.0.1`. The critic has none.
Never use production reader credentials in mutation tests.

## Run locally

Provide random **alphanumeric** `RSS_PASSWORD`, `RSS_API_PASSWORD` and
`SERVICE_TOKEN` through environment or a mode-0600 env file outside git. The
FreshRSS bootstrap parses these as CLI arguments; do not use whitespace/options.
Compute `RSS_FEVER_KEY = md5("reader:" + RSS_API_PASSWORD)` without a newline.
Set `RSS_PORT` if 8787 is occupied. Secrets are not defaults or committed files.

```sh
docker compose -p rss-acceptance -f deploy/compose.yaml up -d reader
# Wait for HTTP and for this command to list reader before importing:
docker compose -p rss-acceptance -f deploy/compose.yaml exec -T --user www-data reader cli/list-users.php
python deploy/feed_opml.py /path/to/feeds/urls > /secure/tmp/intake.opml
docker compose -p rss-acceptance -f deploy/compose.yaml cp /secure/tmp/intake.opml reader:/tmp/intake.opml
docker compose -p rss-acceptance -f deploy/compose.yaml exec -T --user www-data reader cli/import-for-user.php --user reader --filename /tmp/intake.opml
docker compose -p rss-acceptance -f deploy/compose.yaml exec -T --user www-data reader cli/actualize-user.php --user reader
SOURCE_URL=http://127.0.0.1:8787 SOURCE_API_KEY="$RSS_FEVER_KEY" python deploy/check_reader.py --mutate-staging
```

Import preserves all subscription URLs, including temporarily failing feeds.
A registered feed is not proof its last fetch succeeded. Export OPML and compare
URLs; inspect permanent redirects rather than treating changed URLs as missing.
Do not retry a rate-limited source aggressively. A 429 remains an explicit error.

For the critic profile, also supply `JUDGE_API_KEY` and the selected provider's
full endpoint/model/protocol. It refuses to start without source/judge/service
credentials. Leave scheduler off during acceptance.

```sh
docker compose -p rss-acceptance -f deploy/compose.yaml --profile critic up -d --build
# No paid run or email is triggered by starting the stack (interval=0).
```

On NixOS, a uv-managed Python may need
`SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`; do not disable TLS verification.

## Production gate — not executed by the acceptance stack

Promotion touches reader, critic and notification workflow. Obtain the explicit
production gate before executing it. **No shared proxy/tunnel restart is part
of the acceptance test.** Do not delete Miniflux or its database.

1. Verify `nginx -t`, public app health, disk headroom and backups. Export
   Miniflux OPML and read/starred state; OPML alone does **not** migrate read state.
2. Create a dedicated FreshRSS app with persistent `/var/www/FreshRSS/data`,
   explicit `http:80:80` mapping, pinned image and API enabled. Keep private
   during parity checking. Protect bootstrap secrets with rbw and Dokku config;
   configure the same bounded cron refresh as the acceptance stack.
3. Import all 47 declared subscriptions. Compare actual source URLs with the
   old reader, permanent redirects, per-feed errors, category coverage and
   recent item counts. Validate a database backup by restoring to staging.
4. Attach the reader and critic to a dedicated user-defined Docker network with
   stable app aliases, not container IPs. Point `SOURCE_TYPE=freshrss`,
   `SOURCE_URL=http://<reader-alias>`, `SOURCE_API_KEY=<Fever key>` at the new
   reader and give it a separate verdict volume/log. Keep the old settings and
   image ID as rollback evidence. Do not reuse Miniflux IDs in FreshRSS.
5. Keep critic proxy disabled and no host ports. Verify the absence of public
   domains, generated default vhost, tunnel ingress and DNS record. A public
   403 alone is not proof all ingress paths are closed. Remove the residual
   critic DNS/vhost only under its own shared-ingress gate, with nginx validation.
6. Provision n8n credential `glmCriticHeader1` (`X-Critic-Token`) and install the
   fixed workflow. Use the authenticated API to activate without restarting
   n8n/worker; do **not** use current `n8nctl activate`, which restarts both.
   Test unauthorized rejection and a zero-signal payload (no email).
7. Verify a bounded real GLM dry-run, then a single reader write. Only after
   parity, backup/restore and review gates: enable `CRITIC_EVERY_SECONDS=14400`
   and authenticated `NOTIFY_URL`. Test email only with explicit send approval.
8. Promote the reader URL under the proxy gate; keep Miniflux as rollback until
   read/starred-state migration and user-visible parity are confirmed.

Rollback before retirement: disable the critic scheduler, restore prior critic
image/config/log and use the unchanged Miniflux. Revert the code via a revert PR.
Do not run migrations against the old database or remove its volumes.

## Limits that remain gates, not successful checks

- OpenAI Responses and Anthropic Messages have offline contract tests, not live
  funded-provider acceptance. Claude funding is paused; no fallback bypasses it.
- HTTP acknowledgement does not guarantee exactly-once email. Timeout after
  SMTP acceptance may cause a duplicate; use a transactional delivery outbox if
  that guarantee becomes a requirement.
- Precision, usefulness against the deterministic triage and cutoff calibration
  still require a labelled sample. More providers alone do not improve them.
