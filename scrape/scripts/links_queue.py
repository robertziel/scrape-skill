#!/usr/bin/env python3
"""Found-links cache → ranked next-subject candidates for the lead's replan.

  links_queue.py tally <raw_dir> [--top 30] [--per-host 3]
      Reads <raw_dir>/links/<subject>[.<n>].jsonl (one file per worker; the name is the subject,
      `.<n>` = a split worker on the same subject; lines {url, found_on, anchor?, hint?, ts?}),
      drops URLs whose canonical key is in <raw_dir>/seen.txt, groups by host and prints one JSON
      line per host, best first: {host, votes (distinct subjects), n, urls, anchors, hints}.
      A malformed line exits 1 naming file:line — nothing is skipped.
  links_queue.py canon <url> [<url> ...]
      Prints the canonical key of each URL — the form seen.txt holds.
"""
import json
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REQUIRED = ("url", "found_on")


def canon(url):
    p = urlsplit(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"not an http(s) URL: {url!r}")
    host = p.hostname.lower().removeprefix("www.")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")))
    return urlunsplit(("https", host, p.path.rstrip("/") or "/", query, ""))


def host_of(key):
    return urlsplit(key).hostname


def tally(raw_dir, top, per_host):
    raw = Path(raw_dir)
    seen_file = raw / "seen.txt"
    seen = {line.strip() for line in seen_file.read_text().splitlines() if line.strip()} if seen_file.exists() else set()
    hosts = {}
    for f in sorted((raw / "links").glob("*.jsonl")):
        subject = f.name.split(".")[0]
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                missing = [k for k in REQUIRED if not rec.get(k)]
                if missing:
                    raise ValueError(f"missing {missing}")
                key = canon(rec["url"])
            except ValueError as e:  # JSONDecodeError is a ValueError
                sys.exit(f"{f}:{i}: {e}")
            if key in seen:
                continue
            h = hosts.setdefault(host_of(key), {"subjects": set(), "urls": {}, "anchors": [], "hints": []})
            h["subjects"].add(subject)
            h["urls"][key] = None
            for field, bucket in (("anchor", "anchors"), ("hint", "hints")):
                v = rec.get(field)
                if v and v not in h[bucket]:
                    h[bucket].append(v)
    ranked = sorted(hosts.items(), key=lambda kv: (-len(kv[1]["subjects"]), -len(kv[1]["urls"]), kv[0]))
    for host, h in ranked[:top]:
        print(json.dumps({"host": host, "votes": len(h["subjects"]), "n": len(h["urls"]),
                          "urls": list(h["urls"])[:per_host], "anchors": h["anchors"][:per_host],
                          "hints": h["hints"][:per_host]}, ensure_ascii=False))


def main(argv):
    if len(argv) >= 2 and argv[0] == "tally":
        opts = dict(zip(argv[2::2], argv[3::2]))
        unknown = set(opts) - {"--top", "--per-host"}
        if unknown or len(argv[2:]) % 2:
            sys.exit(f"bad options: {argv[2:]}")
        tally(argv[1], int(opts.get("--top", 30)), int(opts.get("--per-host", 3)))
    elif len(argv) >= 2 and argv[0] == "canon":
        for u in argv[1:]:
            print(canon(u))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
