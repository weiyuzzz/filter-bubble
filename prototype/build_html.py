#!/usr/bin/env python3
"""Inline payload.json into viewer.template.html -> worldmonitor-prototype.html

Keeping the data inline is what lets the page work from file:// with no server,
no CDN and no network access.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(HERE, "viewer.template.html")
OUT = os.path.join(HERE, "worldmonitor-prototype.html")

with open(TPL, encoding="utf-8") as fh:
    html = fh.read()

with open(os.path.join(HERE, "payload.json"), encoding="utf-8") as fh:
    payload = fh.read()

if "__PAYLOAD__" not in html:
    raise SystemExit("template marker __PAYLOAD__ not found")

# Guard against a stray "</script>" inside the JSON payload.
assert "</script" not in payload.lower(), "payload would break out of the script tag"

html = html.replace("__PAYLOAD__", payload)
with open(OUT, "w", encoding="utf-8") as fh:
    fh.write(html)
print("wrote", OUT)
print("bytes", os.path.getsize(OUT))