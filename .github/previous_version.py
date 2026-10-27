"""The release before the given one on PyPI (or before the newest, given nothing), as a pip spec:
releases up to 0.9.43 were published as graph-mind-memory, later ones as turnbook."""
import json
import sys
import urllib.request

key = lambda v: tuple(int(p) for p in v.split(".") if p.isdigit())   # noqa: E731
found = {}
for package in ("graph-mind-memory", "turnbook"):
    try:
        releases = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json"))
    except Exception:       # turnbook before its first release
        continue
    found.update({v: package for v, files in releases["releases"].items() if files})
versions = sorted(found, key=key)
target = sys.argv[1].lstrip("v") if len(sys.argv) > 1 and sys.argv[1] else versions[-1]
previous = [v for v in versions if key(v) < key(target)][-1]
print(f"{found[previous]}=={previous}")
