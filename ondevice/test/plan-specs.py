"""Journeys to plan on both sides. Real stop ids per region, several times
of day, and the address form (walking penalties at both ends) that a
dropped pin produces."""
import json
import sys

PAIRS = {
    "seq": [("301440", "10802"), ("301439", "300030"),
            ("10802", "301440"), ("300030", "19233")],
    "mel": [("2:vic:rail:FSS", "2:vic:rail:CXT"),
            ("2:vic:rail:CXT", "2:vic:rail:SSS")],
}
ADDRESS_FORM = {
    "seq": [{"sources": [["301440", 240], ["301439", 300], ["10802", 600]],
             "targets": [["300030", 180], ["19233", 420]], "dep_sec": 8 * 3600},
            {"sources": [["19233", 120]], "targets": [["301440", 540]],
             "dep_sec": 16 * 3600 + 1800}],
}
region = sys.argv[1]
out = [{"from": a, "to": b, "dep_sec": h * 3600 + 900}
       for a, b in PAIRS.get(region, []) for h in (7, 12, 17, 22)]
out += ADDRESS_FORM.get(region, [])
print(json.dumps(out))
