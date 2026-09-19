# Plan

FreshRSS 1.30.0 is the reader: official container, OPML, SQLite for a single user, RSS/Atom/JSON/HTML sources, Fever/GReader clients. Miniflux remains the rollback source. TT-RSS has useful filters/plugins but needs more moving pieces; CommaFeed has a REST API but offers no reason to discard the existing critic. No custom reader image or classification extension is necessary.

Use the native Fever API in a source adapter shared by CLI and service. Fetch unread IDs, then batches of at most 50 explicit IDs (Fever limit), normalize entries, save favorites idempotently. Source-local IDs never cross source caches. Use separate logs per source and include model/endpoint/protocol in cache identity.

Keep the existing OpenAI chat protocol; add explicit Responses and Anthropic Messages wire formats. This is protocol support, not automatic funded access to every vendor. No silent provider fallback, subscription-token conversion or lower-tier defaults.

Add a loopback-only Compose reader plus an unexposed critic, and a subscription-list to OPML converter. Test against the actual pinned image. Dokku promotion must avoid shared nginx/Cloudflare reloads until its gate is approved. The n8n fix is a separate repo: stable webhookId + header authentication, no runtime restart during this slice.
