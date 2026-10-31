#!/usr/bin/env python3
"""
Find which ATS board a company uses by probing the public APIs with candidate slugs.

  python3 discover.py "Moment Energy" momentenergy moment-energy
  python3 discover.py "Some Co"            # slugs guessed from the name

Prints every (ats, slug) that answers with a valid board and its job count, plus a
ready-to-paste config.toml block for the best hit.
"""
import re
import sys
import concurrent.futures as cf

import watcher as w

import os
PROBES = [a for a in ["greenhouse", "lever", "ashby", "workable", "bamboohr", "recruitee", "breezy", "smartrecruiters", "rippling"]
          if a not in os.environ.get("SKIP_ATS", "").split(",")]


def guesses(name):
    base = re.sub(r"\b(inc|ltd|corp|corporation|technologies|technology|systems|limited|co)\b\.?", "",
                  name.lower())
    words = re.findall(r"[a-z0-9]+", base)
    full = re.findall(r"[a-z0-9]+", name.lower())
    out = ["".join(words), "-".join(words), "".join(full), "-".join(full), words[0] if words else ""]
    if len(words) > 1:
        out += [words[0] + words[1], "".join(words) + "inc", "".join(words) + "technologies"]
    return [g for g in dict.fromkeys(out) if g]


def probe(ats, slug):
    c = {"name": slug, "ats": ats, "id": slug}
    try:
        jobs, _ = w.ADAPTERS[ats](c)
        return ats, slug, len(jobs), (jobs[0].title if jobs else "")
    except Exception as e:  # noqa: BLE001
        return ats, slug, None, str(e)[:60]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    name, slugs = sys.argv[1], sys.argv[2:] or guesses(sys.argv[1])
    hits = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for ats, slug, n, info in ex.map(lambda p: probe(*p), [(a, s) for s in slugs for a in PROBES]):
            if n is not None and not (n == 0 and ats in ("smartrecruiters", "rippling")):
                hits.append((n, ats, slug, info))
    hits.sort(reverse=True)
    if not hits:
        print(f"{name}: no board found for slugs {slugs}")
        return 1
    for n, ats, slug, info in hits:
        print(f"{name}: {ats:<16} {slug:<28} {n:>5} jobs   e.g. {info[:60]}")
    n, ats, slug, _ = hits[0]
    print(f'\n[[companies]]\nname = "{name}"\nats = "{ats}"\nid = "{slug}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
