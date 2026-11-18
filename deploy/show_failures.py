"""Print why the offline checks failed: the errors list and the first test failure. Used by setup-car.sh."""
import json
import sys

text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
start = text.rfind('{"offline_preflight_pass"')
if start < 0:
    start = text.rfind('"offline_preflight_pass"')
    start = text.rfind("{", 0, start)
if start >= 0:
    try:
        for error in json.JSONDecoder().raw_decode(text[start:])[0].get("errors", []):
            print("    " + str(error))
    except ValueError:
        pass
first = text.find("=" * 70)
if first >= 0:
    print("\n".join("    " + line for line in text[first:].splitlines()[:40]))
