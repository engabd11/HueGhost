"""The defaults table in docs/settings.md must say what the code does."""
import json
import os
import re

from hueghost.config import Config

DOC = os.path.join(os.path.dirname(__file__), "..", "docs", "settings.md")


def test_the_documented_defaults_are_the_real_ones():
    rows = re.findall(r"\| `([a-z_.]+)` \| `([^`]+)` \|", open(DOC, encoding="utf-8").read())
    assert len(rows) > 20                        # the table is still there, in this shape
    cfg = Config({})
    wrong = {k: (json.loads(v), cfg.get(k)) for k, v in rows if json.loads(v) != cfg.get(k)}
    assert not wrong, "docs/settings.md (documented, real): %s" % wrong
