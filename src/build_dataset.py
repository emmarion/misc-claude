#!/usr/bin/env python3
"""Build a reference dataset of WikiTree "Gateway Ancestors".

Gateway Ancestors are colonial immigrants whose descent from medieval royalty /
the Magna Carta surety barons has been documented and *peer-reviewed*. The
WikiTree "Gateway Ancestors" category only contains profiles whose trail back to
a surety baron has been reviewed and "badged" by the Magna Carta Project, and is
keyed to Douglas Richardson's *Magna Carta Ancestry* / *Royal Ancestry* -- the
current scholarly gold standard.

This script:
  1. (optionally) re-scrapes the category page to refresh the list of member IDs
  2. fetches name + birth/death date + place for each member via the WikiTree API
  3. writes data/gateway_ancestors.csv and data/gateway_ancestors.json

Only the Python standard library is required.

The resulting dataset is the *target* list: people to look for in your own tree.
Finding one is the start of the proof, not the proof itself -- you still have to
document every parent-child link between you and the gateway ancestor.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.wikitree.com/api.php"
CATEGORY = "Gateway_Ancestors"
USER_AGENT = "gateway-royalty-research/1.0 (+https://www.wikitree.com; personal genealogy research)"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
IDS_FILE = os.path.join(DATA, "gateway_ancestor_ids.txt")
CSV_OUT = os.path.join(DATA, "gateway_ancestors.csv")
JSON_OUT = os.path.join(DATA, "gateway_ancestors.json")

FIELDS = [
    "Name", "FirstName", "MiddleName", "LastNameAtBirth", "RealName",
    "BirthDate", "DeathDate", "BirthLocation", "DeathLocation",
]


def _get(url, tries=6, base_delay=4.0):
    """HTTP GET with exponential backoff on rate limits / transient errors."""
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", "replace")
            # The WikiTree API answers 200 even for "Limit exceeded"; detect it.
            if '"Limit exceeded"' in body or "Limit exceeded" in body:
                raise RuntimeError("rate limited (Limit exceeded)")
            return body
        except Exception as e:  # noqa: BLE001 - we want to retry everything transient
            last = e
            delay = base_delay * (2 ** attempt)
            sys.stderr.write(f"  ! {e} -- retry in {delay:.0f}s ({attempt+1}/{tries})\n")
            time.sleep(delay)
    raise SystemExit(f"giving up after {tries} tries: {last}")


def scrape_ids():
    """Return the sorted set of member profile IDs in the category.

    Category listings paginate with &from=<letter>, so we sweep A-Z and union.
    """
    ids = set()
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        url = (
            "https://www.wikitree.com/index.php?title=Category:"
            f"{CATEGORY}&from={letter}"
        )
        html = _get(url)
        for m in re.findall(r"/wiki/([A-Za-z_]+-\d+)", html):
            ids.add(m)
        time.sleep(1.0)  # be polite to the web host
    return sorted(ids)


def load_ids():
    with open(IDS_FILE, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip()]


def fetch_people(ids, batch=100, pause=3.0):
    """Fetch profile fields for ids via the bulk getPeople action."""
    out = {}
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        params = {
            "action": "getPeople",
            "keys": ",".join(chunk),
            "fields": ",".join(FIELDS),
            "format": "json",
        }
        url = API + "?" + urllib.parse.urlencode(params)
        sys.stderr.write(f"fetching {i+1}-{i+len(chunk)} of {len(ids)}...\n")
        data = json.loads(_get(url))
        people = _extract_people(data)
        out.update(people)
        time.sleep(pause)
    return out


def _extract_people(data):
    """getPeople returns [{... "people": {id: {fields}}}]; be defensive."""
    if isinstance(data, list):
        for el in data:
            if isinstance(el, dict) and isinstance(el.get("people"), dict):
                return {k: v for k, v in el["people"].items() if isinstance(v, dict)}
    if isinstance(data, dict) and isinstance(data.get("people"), dict):
        return data["people"]
    return {}


def year_of(date_str):
    """Extract a 4-digit year from a WikiTree date ('YYYY-MM-DD', partials, '0000')."""
    if not date_str:
        return ""
    m = re.match(r"(\d{4})", date_str)
    if not m:
        return ""
    y = m.group(1)
    return "" if y == "0000" else y


def build_rows(people):
    rows = []
    for pid, p in people.items():
        rows.append({
            "wikitree_id": pid,
            "name": p.get("RealName") or " ".join(
                x for x in (p.get("FirstName"), p.get("MiddleName"),
                            p.get("LastNameAtBirth")) if x),
            "first_name": p.get("FirstName", ""),
            "middle_name": p.get("MiddleName", ""),
            "last_name_at_birth": p.get("LastNameAtBirth", ""),
            "birth_date": p.get("BirthDate", ""),
            "birth_year": year_of(p.get("BirthDate", "")),
            "birth_location": p.get("BirthLocation", ""),
            "death_date": p.get("DeathDate", ""),
            "death_year": year_of(p.get("DeathDate", "")),
            "death_location": p.get("DeathLocation", ""),
            "wikitree_url": f"https://www.wikitree.com/wiki/{pid}",
        })
    rows.sort(key=lambda r: (r["last_name_at_birth"], r["first_name"], r["wikitree_id"]))
    return rows


def write_outputs(rows):
    os.makedirs(DATA, exist_ok=True)
    cols = ["wikitree_id", "name", "first_name", "middle_name", "last_name_at_birth",
            "birth_date", "birth_year", "birth_location",
            "death_date", "death_year", "death_location", "wikitree_url"]
    with open(CSV_OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    with open(JSON_OUT, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)
    print(f"wrote {len(rows)} gateway ancestors")
    print(f"  {CSV_OUT}")
    print(f"  {JSON_OUT}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh-ids", action="store_true",
                    help="re-scrape the category for member IDs before fetching")
    ap.add_argument("--batch", type=int, default=100, help="profiles per API call")
    ap.add_argument("--pause", type=float, default=3.0, help="seconds between batches")
    args = ap.parse_args()

    if args.refresh_ids:
        ids = scrape_ids()
        os.makedirs(DATA, exist_ok=True)
        with open(IDS_FILE, "w", encoding="utf-8") as fh:
            fh.write("\n".join(ids) + "\n")
        print(f"refreshed {len(ids)} ids -> {IDS_FILE}")
    else:
        ids = load_ids()
        print(f"loaded {len(ids)} ids from {IDS_FILE}")

    people = fetch_people(ids, batch=args.batch, pause=args.pause)
    rows = build_rows(people)
    write_outputs(rows)


if __name__ == "__main__":
    main()
