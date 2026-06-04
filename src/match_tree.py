#!/usr/bin/env python3
"""Match people in YOUR family tree against the Gateway Ancestor dataset.

Input: a GEDCOM export of your tree (see README for how to get one out of
FamilySearch) and data/gateway_ancestors.csv produced by build_dataset.py.

It fuzzy-matches every individual in your GEDCOM against the gateway list by
surname + given name + birth year, and ranks the hits. A strong hit means
"this person in your tree *might* be a documented gateway ancestor" -- your
job is then to verify, link by link, that the person in your tree really is
that person (same dates, same place, same spouse), and that every parent-child
step between you and them is supported by records. The match is a lead, not a
proof.

Standard library only.
"""

import argparse
import csv
import os
import re
import sys
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CSV = os.path.join(ROOT, "data", "gateway_ancestors.csv")
DEFAULT_OUT = os.path.join(ROOT, "data", "matches.csv")


# --------------------------------------------------------------------------- #
# GEDCOM parsing (minimal, tolerant)                                          #
# --------------------------------------------------------------------------- #
def parse_gedcom(path):
    """Yield dicts for each INDI: {id, given, surname, birth_year, birth_place,
    death_year, death_place, full_name}."""
    people = []
    cur = None
    section = None  # which level-1 event we're inside (BIRT/DEAT/...)
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            parts = line.split(" ", 2)
            try:
                level = int(parts[0])
            except ValueError:
                continue
            tag = parts[1] if len(parts) > 1 else ""
            val = parts[2] if len(parts) > 2 else ""

            if level == 0:
                if cur is not None:
                    people.append(cur)
                cur = None
                section = None
                if tag.startswith("@") and val == "INDI":
                    cur = {"id": tag.strip("@"), "given": "", "surname": "",
                           "full_name": "", "birth_year": "", "birth_place": "",
                           "death_year": "", "death_place": ""}
                continue

            if cur is None:
                continue

            if level == 1:
                section = tag
                if tag == "NAME" and val:
                    _set_name(cur, val)
            elif level == 2 and cur is not None:
                if tag == "GIVN" and val and not cur["given"]:
                    cur["given"] = val
                elif tag == "SURN" and val and not cur["surname"]:
                    cur["surname"] = val
                elif tag == "DATE":
                    y = _year(val)
                    if section == "BIRT" and y:
                        cur["birth_year"] = y
                    elif section == "DEAT" and y:
                        cur["death_year"] = y
                elif tag == "PLAC":
                    if section == "BIRT":
                        cur["birth_place"] = val
                    elif section == "DEAT":
                        cur["death_place"] = val
        if cur is not None:
            people.append(cur)
    return people


def _set_name(rec, val):
    rec["full_name"] = val.replace("/", " ").strip()
    m = re.search(r"/([^/]*)/", val)
    if m:
        rec["surname"] = rec["surname"] or m.group(1).strip()
        given = (val[:m.start()] + " " + val[m.end():]).strip()
        rec["given"] = rec["given"] or given


def _year(date_val):
    m = re.search(r"\b(\d{4})\b", date_val or "")
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- #
# Normalization + scoring                                                      #
# --------------------------------------------------------------------------- #
def norm(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def ratio(a, b):
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def first_token(s):
    n = norm(s)
    return n.split(" ")[0] if n else ""


def score(person, gw, year_window):
    """Return (score 0..1, reason) for a candidate match, or (0, '')."""
    p_sur, g_sur = norm(person["surname"]), norm(gw["last_name_at_birth"])
    sur_sim = ratio(p_sur, g_sur)
    if sur_sim < 0.80:  # surname is the anchor; reject weak surname matches early
        return 0.0, ""

    p_giv, g_giv = norm(person["given"]), norm(gw["first_name"])
    giv_full = ratio(p_giv, g_giv)
    giv_first = ratio(first_token(person["given"]), first_token(gw["first_name"]))
    giv_sim = max(giv_full, giv_first)

    # Birth-year agreement
    py, gy = person["birth_year"], gw["birth_year"]
    if py and gy:
        diff = abs(int(py) - int(gy))
        if diff > year_window:
            year_score = 0.0
        else:
            year_score = 1.0 - (diff / (year_window + 1))
        year_known = True
    else:
        year_score = 0.5  # unknown on one side: neither reward nor punish hard
        year_known = False

    # Weighted blend: surname and given name carry the identity; year confirms.
    s = 0.40 * sur_sim + 0.35 * giv_sim + 0.25 * year_score

    # A confident hit needs a real given-name match, not just a common surname.
    if giv_sim < 0.55:
        s *= 0.5

    bits = [f"surname {sur_sim:.2f}", f"given {giv_sim:.2f}"]
    if year_known:
        bits.append(f"birthyr |{py}-{gy}|={abs(int(py)-int(gy))}")
    else:
        bits.append("birthyr n/a")
    return s, ", ".join(bits)


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def load_gateways(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gedcom", help="path to your GEDCOM (.ged) export")
    ap.add_argument("--gateways", default=DEFAULT_CSV, help="gateway_ancestors.csv")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output CSV of matches")
    ap.add_argument("--min-score", type=float, default=0.62,
                    help="report matches at or above this score (0..1)")
    ap.add_argument("--year-window", type=int, default=8,
                    help="max birth-year gap still considered a match")
    ap.add_argument("--top", type=int, default=40, help="rows to print to console")
    args = ap.parse_args()

    if not os.path.exists(args.gateways):
        sys.exit(f"gateway dataset not found: {args.gateways}\n"
                 f"run  python3 src/build_dataset.py  first.")

    people = parse_gedcom(args.gedcom)
    gateways = load_gateways(args.gateways)
    print(f"parsed {len(people)} individuals from {args.gedcom}")
    print(f"comparing against {len(gateways)} gateway ancestors\n")

    matches = []
    for person in people:
        if not person["surname"]:
            continue
        for gw in gateways:
            s, reason = score(person, gw, args.year_window)
            if s >= args.min_score:
                matches.append({
                    "score": round(s, 3),
                    "your_person": person["full_name"] or person["given"],
                    "your_birth": person["birth_year"],
                    "your_birth_place": person["birth_place"],
                    "gateway_name": gw["name"],
                    "gateway_birth": gw["birth_year"],
                    "gateway_birth_place": gw["birth_location"],
                    "wikitree_url": gw["wikitree_url"],
                    "why": reason,
                })

    matches.sort(key=lambda m: m["score"], reverse=True)

    cols = ["score", "your_person", "your_birth", "your_birth_place",
            "gateway_name", "gateway_birth", "gateway_birth_place",
            "wikitree_url", "why"]
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(matches)

    if not matches:
        print("No candidate matches at this threshold. Try lowering --min-score,\n"
              "or widening --year-window. Remember: no match here just means none\n"
              "of *these* reviewed Magna Carta gateways appear in your tree.")
        return

    print(f"found {len(matches)} candidate match(es); top {min(args.top, len(matches))}:\n")
    for m in matches[:args.top]:
        print(f"  [{m['score']:.2f}] {m['your_person']} (b.{m['your_birth'] or '?'})"
              f"  ~  {m['gateway_name']} (b.{m['gateway_birth'] or '?'})")
        print(f"         {m['why']}")
        print(f"         {m['wikitree_url']}")
    print(f"\nfull results -> {args.out}")
    print("\nNEXT: open each WikiTree link, confirm it's truly the same person as in\n"
          "your tree (dates/place/spouse), then verify every parent-child link\n"
          "between you and them with records. The match is a lead, not a proof.")


if __name__ == "__main__":
    main()
