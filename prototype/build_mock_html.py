#!/usr/bin/env python3
"""Inline the real styles.css + mock-payload.json into mock-radar.template.html."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
TPL = os.path.join(HERE, "mock-radar.template.html")
OUT = os.path.join(HERE, "radar-map-mockup.html")

html = open(TPL, encoding="utf-8").read()
css = open(os.path.join(ROOT, "web", "styles.css"), encoding="utf-8").read()
payload = open(os.path.join(HERE, "mock-payload.json"), encoding="utf-8").read()

for marker in ("__STYLES__", "__PAYLOAD__"):
    assert marker in html, "missing marker %s" % marker
assert "</style" not in css.lower(), "styles.css would break out of the style tag"
assert "</script" not in payload.lower(), "payload would break out of the script tag"

html = html.replace("__STYLES__", css).replace("__PAYLOAD__", payload)
open(OUT, "w", encoding="utf-8").write(html)
print("wrote", OUT)
print("bytes", os.path.getsize(OUT))
print("css inlined:", len(css), "| payload:", len(payload))