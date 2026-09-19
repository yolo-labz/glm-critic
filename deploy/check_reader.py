"""Read a staging FreshRSS and verify reversible star semantics. No LLM/email.

Requires SOURCE_URL and SOURCE_API_KEY, plus explicit --mutate-staging.
Run only against a disposable reader. Production endpoints are not test fixtures.
"""

import argparse
import json
import os
from urllib.parse import urlsplit

from glm_critic.sources import FreshRSSSource, HttpSource


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutate-staging", action="store_true", required=True)
    args = parser.parse_args()
    url = os.environ["SOURCE_URL"]
    if urlsplit(url).hostname not in {"localhost", "127.0.0.1", "reader"}:
        parser.error("only the isolated reader or loopback is accepted")
    source = FreshRSSSource(HttpSource(url, os.environ["SOURCE_API_KEY"]))
    feeds = source.feeds()
    items, total = source.entries(limit=70)
    if not items:
        raise RuntimeError("import and refresh at least one feed first")
    entry = items[0]
    before = entry["starred"]
    try:
        assert args.mutate_staging
        assert source.star(entry["id"])
        assert source.star(entry["id"]), "second save must not toggle the star off"
    finally:
        assert source.star(entry["id"], on=before), "restore original favorite state"
    print(
        json.dumps(
            {
                "feeds": len(feeds),
                "unread": total,
                "fetched": len(items),
                "star_twice": "passed",
                "restored": True,
            }
        )
    )


if __name__ == "__main__":
    main()
