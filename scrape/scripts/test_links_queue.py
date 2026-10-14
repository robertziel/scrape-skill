"""python3 -m unittest discover -s scrape/scripts"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import links_queue as lq


def run_tally(raw, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        lq.tally(raw, kw.get("top", 30), kw.get("per_host", 3))
    return [json.loads(line) for line in buf.getvalue().splitlines()]


class LinksQueueTest(unittest.TestCase):
    def setUp(self):
        self.raw = Path(tempfile.mkdtemp())
        (self.raw / "links").mkdir()

    def write(self, name, *recs):
        with open(self.raw / "links" / name, "a") as f:
            for r in recs:
                f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")

    def test_canon(self):
        self.assertEqual(lq.canon("HTTP://www.Acme.com/jobs/?utm_source=x&b=2&a=1#top"),
                         "https://acme.com/jobs?a=1&b=2")
        with self.assertRaises(ValueError):
            lq.canon("mailto:hi@acme.com")

    def test_votes_rank_and_seen(self):
        self.write("acme.jsonl", {"url": "https://boards.example/acme", "found_on": "https://a.com"},
                   {"url": "https://lone.example/x", "found_on": "https://a.com"})
        self.write("acme.2.jsonl", {"url": "https://boards.example/acme2", "found_on": "https://a.com"})
        self.write("beta.jsonl", {"url": "https://boards.example/beta", "found_on": "https://b.com",
                                  "anchor": "Careers"},
                   {"url": "https://seen.example/", "found_on": "https://b.com"})
        (self.raw / "seen.txt").write_text(lq.canon("https://seen.example") + "\n")
        out = run_tally(self.raw)
        self.assertEqual([o["host"] for o in out], ["boards.example", "lone.example"])
        self.assertEqual((out[0]["votes"], out[0]["n"], out[0]["anchors"]), (2, 3, ["Careers"]))

    def test_malformed_line_fails_loudly(self):
        self.write("w1.jsonl", {"url": "https://ok.example", "found_on": "https://a.com"},
                   {"url": "https://no-found-on.example"})
        with self.assertRaises(SystemExit) as e:
            run_tally(self.raw)
        self.assertIn("w1.jsonl:2", str(e.exception.code))


if __name__ == "__main__":
    unittest.main()
