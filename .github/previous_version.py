"""The release before the given one on PyPI (or before the newest, given nothing)."""
import json
import sys
import urllib.request

releases = json.load(urllib.request.urlopen(
    "https://pypi.org/pypi/graph-mind-memory/json"))["releases"]
key = lambda v: tuple(int(p) for p in v.split(".") if p.isdigit())   # noqa: E731
versions = sorted((v for v, files in releases.items() if files), key=key)
target = sys.argv[1].lstrip("v") if len(sys.argv) > 1 and sys.argv[1] else versions[-1]
print([v for v in versions if key(v) < key(target)][-1])
