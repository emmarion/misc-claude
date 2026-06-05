#!/usr/bin/env python3
"""Export your direct ancestral line (pedigree) from FamilySearch.

Why a *pedigree* and not the whole tree: to look for gateway ancestors you only
need your direct ancestors -- not cousins or descendants. Pedigree doubles every
generation, so this walks UP with cutoffs that prune branches which cannot
contain a colonial gateway (born ~1570-1697):

  --max-generations   hard depth cap (default 15)
  --stop-before-year  do not expand ancestors born before this year. A gateway
                      IS the immigrant, born 1570-1697, so the year floor is the
                      primary stopper: climb until births reach the bottom of
                      that window, then stop. Default 1560 (just below the oldest
                      gateway) brackets the whole window. Birth years are more
                      reliably recorded than places, so this -- not the country
                      filter -- does the real work.
  --birth-countries   optional, DESCENDANT-SIDE allow-list (the Americas), to
                      halt a line at the ocean crossing. A gateway is always the
                      immigrant, so the first British-born ancestor on a line is
                      the deepest point of interest; there are no gateways further
                      up (their parents never emigrated). Recording is NOT gated
                      by this filter -- the immigrant is still captured in the
                      chunk triggered by their American-born child -- so an
                      Americas-only list stops climbing right at the crossing.
                      Do NOT add Britain: that makes you climb past the gateway
                      into ancestry the books already cover. Off by default; its
                      only real job is cheaply pruning non-British branches (e.g.
                      a German line that immigrated in 1840). When matching the
                      broader non-British gateways (Dutch/French/German), widen
                      it to their origin or omit it. Pruning on place is risky --
                      colonial place strings are messy -- so lean on the floor.

Output: data/my_pedigree.csv (id, generation, name, surname, birth_year,
birth_place, birth_country), and optionally a GEDCOM with --gedcom.

Auth: supply an OAuth access token via --token or $FS_ACCESS_TOKEN, or run the
built-in Authorization-Code+PKCE login with --login --client-id <APP_KEY>
(register http://localhost:8642/callback as a redirect URI in your FS app).

Standard library only. The network/auth layer is isolated from the traversal +
cutoff logic so the latter can be unit-tested with fixtures (see tests/).
"""

import argparse
import base64
import csv
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")

# FamilySearch hosts (production by default; --beta switches to the sandbox).
HOSTS = {
    "prod": {"api": "https://api.familysearch.org",
             "ident": "https://ident.familysearch.org"},
    "beta": {"api": "https://apibeta.familysearch.org",
             "ident": "https://identbeta.familysearch.org"},
}
GEDCOMX = "application/x-gedcomx-v1+json"
REDIRECT_URI = "http://localhost:8642/callback"


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #
def _retry_after(http_error):
    """Seconds to wait per a 429/503 Retry-After header (int seconds or HTTP
    date), or 0.0 if absent/unparseable."""
    val = http_error.headers.get("Retry-After") if http_error.headers else None
    if not val:
        return 0.0
    try:
        return float(val)
    except ValueError:
        from email.utils import parsedate_to_datetime
        from datetime import datetime
        try:
            dt = parsedate_to_datetime(val)
            return max(0.0, (dt - datetime.now(dt.tzinfo)).total_seconds())
        except (TypeError, ValueError):
            return 0.0


def http_get(url, token, accept=GEDCOMX, tries=5, base_delay=2.0):
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "User-Agent": "gateway-royalty-research/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", "replace")
            if not body.strip():
                raise RuntimeError("empty body")
            return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504):
                # Honor the server's Retry-After when present; otherwise back off
                # exponentially. Never wait less than the server asks, and cap it.
                backoff = base_delay * (2 ** attempt)
                delay = min(max(backoff, _retry_after(e)), 120.0)
                src = "Retry-After" if _retry_after(e) else "backoff"
                sys.stderr.write(f"  ! HTTP {e.code}; retry in {delay:.0f}s ({src})\n")
                time.sleep(delay)
                continue
            if e.code == 204:  # no content (e.g. no ancestry) -> treat as empty
                return {}
            if e.code in (401, 403):
                raise SystemExit(
                    f"HTTP {e.code}: token expired or unauthorized. Re-authenticate "
                    "and run the same command again -- the on-disk cache preserves "
                    "everything already fetched, so it resumes where it stopped.")
            raise
        except Exception as e:  # noqa: BLE001
            delay = base_delay * (2 ** attempt)
            sys.stderr.write(f"  ! {e}; retry in {delay:.0f}s\n")
            time.sleep(delay)
    raise SystemExit(f"GET failed after {tries} tries: {url}")


# --------------------------------------------------------------------------- #
# OAuth (Authorization Code + PKCE)                                            #
# --------------------------------------------------------------------------- #
class _CodeCatcher(BaseHTTPRequestHandler):
    code = None

    def do_GET(self):  # noqa: N802
        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        _CodeCatcher.code = (params.get("code") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h3>Authorized. You can close this tab.</h3>")

    def log_message(self, *a):  # silence
        pass


def oauth_login(client_id, hosts):
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    auth_url = hosts["ident"] + "/cis-web/oauth2/v3/authorization?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id,
        "redirect_uri": REDIRECT_URI, "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print("Opening browser to authorize with FamilySearch...")
    print(auth_url)
    webbrowser.open(auth_url)
    srv = HTTPServer(("localhost", 8642), _CodeCatcher)
    srv.handle_request()  # blocks for the single redirect
    code = _CodeCatcher.code
    if not code:
        raise SystemExit("did not receive an authorization code")
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT_URI, "client_id": client_id,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(hosts["ident"] + "/cis-web/oauth2/v3/token", data=data)
    with urllib.request.urlopen(req, timeout=60) as resp:
        tok = json.loads(resp.read().decode())
    return tok["access_token"]


# --------------------------------------------------------------------------- #
# Parsing (pure -- unit-testable with fixtures)                               #
# --------------------------------------------------------------------------- #
def _year(date_obj):
    """Pull a 4-digit year from a GEDCOM X date (formal '+1605' or original text)."""
    if not isinstance(date_obj, dict):
        return ""
    formal = date_obj.get("formal", "")
    import re
    m = re.search(r"(\d{4})", formal) or re.search(r"\b(\d{4})\b", date_obj.get("original", "") or "")
    return m.group(1) if m else ""


def _name_parts(person):
    """Return (given, surname, full) from a GEDCOM X person."""
    given = surname = ""
    for nm in person.get("names", []) or []:
        for form in nm.get("nameForms", []) or []:
            for part in form.get("parts", []) or []:
                t = part.get("type", "")
                if t.endswith("Given") and not given:
                    given = part.get("value", "")
                elif t.endswith("Surname") and not surname:
                    surname = part.get("value", "")
        if given or surname:
            break
    full = (person.get("display", {}) or {}).get("name", "") or f"{given} {surname}".strip()
    if not surname and full:
        surname = full.split()[-1]
        given = given or " ".join(full.split()[:-1])
    return given.strip(), surname.strip(), full.strip()


def _birth(person):
    """Return (birth_year, birth_place) from display or facts."""
    disp = person.get("display", {}) or {}
    year = ""
    if disp.get("birthDate"):
        import re
        m = re.search(r"\b(\d{4})\b", disp["birthDate"])
        year = m.group(1) if m else ""
    place = disp.get("birthPlace", "") or ""
    if not year or not place:
        for fact in person.get("facts", []) or []:
            if str(fact.get("type", "")).endswith("Birth"):
                year = year or _year(fact.get("date", {}))
                place = place or (fact.get("place", {}) or {}).get("original", "")
    return year, place


def _country(place):
    return place.split(",")[-1].strip() if place else ""


def parse_ancestry(gedcomx, gen_offset=0):
    """Turn an FS ancestry response into person dicts keyed by FS id.

    ascendancyNumber is Ahnentafel: 1=root(gen0), 2/3=parents(gen1), ...
    generation = bit_length(ascNum) - 1, shifted by gen_offset for re-rooting.
    """
    out = {}
    for p in gedcomx.get("persons", []) or []:
        pid = p.get("id")
        if not pid:
            continue
        disp = p.get("display", {}) or {}
        asc = disp.get("ascendancyNumber")
        try:
            gen = (int(asc).bit_length() - 1) + gen_offset if asc else gen_offset
        except (TypeError, ValueError):
            gen = gen_offset
        given, surname, full = _name_parts(p)
        byr, bplace = _birth(p)
        out[pid] = {
            "id": pid, "generation": gen, "full_name": full,
            "given": given, "surname": surname, "birth_year": byr,
            "birth_place": bplace, "birth_country": _country(bplace),
            "ascendancy": asc,
        }
    return out


# --------------------------------------------------------------------------- #
# Cutoff logic (pure)                                                          #
# --------------------------------------------------------------------------- #
def should_expand(person, max_generations, stop_before_year, birth_countries):
    """Decide whether to fetch this person's *ancestors*. This gates climbing,
    not recording -- the person themselves is already captured. Missing data =>
    keep going (never prune on absence)."""
    if person["generation"] >= max_generations:
        return False
    byr = person["birth_year"]
    if byr and int(byr) < stop_before_year:
        # Primary stopper: once a line reaches below the gateway window the
        # gateways on it are already captured, so stop climbing.
        return False
    if birth_countries and person["birth_place"]:
        place = person["birth_place"].lower()
        if not any(c.lower().strip() in place for c in birth_countries):
            # Descendant-side (Americas) list: a British-born ancestor falls
            # through here and halts the climb at the ocean crossing -- which is
            # correct, because the immigrant (already recorded) IS the gateway and
            # nothing above them on this line can be one.
            return False
    return True


# --------------------------------------------------------------------------- #
# Disk cache (also gives resume: a re-run replays cache hits, then continues)  #
# --------------------------------------------------------------------------- #
def _cache_path(cache_dir, pid):
    safe = urllib.parse.quote(pid, safe="")
    return os.path.join(cache_dir, f"{safe}.json")


def cache_read(cache_dir, pid):
    if not cache_dir:
        return None
    path = _cache_path(cache_dir, pid)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None  # corrupt/partial cache entry -> refetch
    return None


def cache_write(cache_dir, pid, data):
    if not cache_dir:
        return
    os.makedirs(cache_dir, exist_ok=True)
    path = _cache_path(cache_dir, pid)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:  # atomic: never leave half files
        json.dump(data, fh)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Traversal                                                                    #
# --------------------------------------------------------------------------- #
def fetch_ancestry(pid, token, hosts, generations=8, cache_dir=None,
                   refresh=False, pause=0.0, stats=None):
    """Fetch an 8-gen ancestry chunk for pid, serving from disk cache if present.

    Returns (data, from_cache). Network calls (cache misses) are cached and
    paused; cache hits are instant -- which is what makes a re-run resume.
    """
    if not refresh:
        cached = cache_read(cache_dir, pid)
        if cached is not None:
            if stats is not None:
                stats["cache_hits"] += 1
            return cached, True
    url = hosts["api"] + "/platform/tree/ancestry?" + urllib.parse.urlencode({
        "person": pid, "generations": generations, "personDetails": "true",
    })
    data = http_get(url, token, GEDCOMX)
    cache_write(cache_dir, pid, data)
    if stats is not None:
        stats["fetched"] += 1
    if pause:
        time.sleep(pause)
    return data, False


def current_person_id(token, hosts):
    data = http_get(hosts["api"] + "/platform/tree/current-person", token, GEDCOMX)
    persons = data.get("persons") or []
    if not persons:
        raise SystemExit("could not resolve current person; pass --root <PID>")
    return persons[0]["id"]


def export_pedigree(root_pid, token, hosts, max_generations, stop_before_year,
                    birth_countries, pause=0.5, cache_dir=None, refresh=False):
    """BFS up the pedigree, re-rooting ancestry calls (FS caps at 8 gens/call).

    A disk cache keyed by person id makes this resumable: if it stops (token
    expiry, rate limit, Ctrl-C), re-running the same command replays the cached
    chunks instantly and only fetches what is still missing.
    """
    people = {}                      # id -> person dict (min generation kept)
    frontier = [(root_pid, 0)]       # (pid, generation) still to expand
    expanded = set()
    stats = {"cache_hits": 0, "fetched": 0}
    while frontier:
        pid, base_gen = frontier.pop(0)
        if pid in expanded:
            continue
        expanded.add(pid)
        data, from_cache = fetch_ancestry(pid, token, hosts, cache_dir=cache_dir,
                                          refresh=refresh, pause=pause, stats=stats)
        sys.stderr.write(f"{'cache' if from_cache else 'fetch'} {pid} (gen {base_gen})\n")
        chunk = parse_ancestry(data, gen_offset=base_gen)
        for cid, person in chunk.items():
            prev = people.get(cid)
            if prev is None or person["generation"] < prev["generation"]:
                people[cid] = person
        # The chunk's deepest persons become the next frontier (re-root there).
        max_gen_in_chunk = max((p["generation"] for p in chunk.values()), default=base_gen)
        for cid, person in chunk.items():
            if cid in expanded:
                continue
            # Only re-root at the leaves of this 8-gen chunk to avoid redundant calls.
            if person["generation"] >= max_gen_in_chunk and should_expand(
                    person, max_generations, stop_before_year, birth_countries):
                frontier.append((cid, person["generation"]))
    sys.stderr.write(f"ancestry chunks: {stats['fetched']} fetched, "
                     f"{stats['cache_hits']} from cache\n")
    return people


# --------------------------------------------------------------------------- #
# Output                                                                       #
# --------------------------------------------------------------------------- #
def write_csv(people, path):
    cols = ["id", "generation", "full_name", "given", "surname",
            "birth_year", "birth_place", "birth_country"]
    rows = sorted(people.values(), key=lambda p: (p["generation"], p["surname"]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def write_gedcom(people, path):
    rows = sorted(people.values(), key=lambda p: p["generation"])
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("0 HEAD\n1 SOUR gateway-royalty-research\n1 GEDC\n2 VERS 5.5.1\n1 CHAR UTF-8\n")
        for i, p in enumerate(rows, 1):
            fh.write(f"0 @I{i}@ INDI\n")
            fh.write(f"1 NAME {p['given']} /{p['surname']}/\n")
            if p["birth_year"] or p["birth_place"]:
                fh.write("1 BIRT\n")
                if p["birth_year"]:
                    fh.write(f"2 DATE {p['birth_year']}\n")
                if p["birth_place"]:
                    fh.write(f"2 PLAC {p['birth_place']}\n")
        fh.write("0 TRLR\n")


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token", default=os.environ.get("FS_ACCESS_TOKEN"),
                    help="FamilySearch OAuth access token (or $FS_ACCESS_TOKEN)")
    ap.add_argument("--login", action="store_true",
                    help="run interactive OAuth login (needs --client-id)")
    ap.add_argument("--client-id", default=os.environ.get("FS_CLIENT_ID"),
                    help="your FamilySearch app key (for --login)")
    ap.add_argument("--beta", action="store_true", help="use the FS sandbox hosts")
    ap.add_argument("--root", help="root person FS id (default: the logged-in user)")
    ap.add_argument("--max-generations", type=int, default=15)
    ap.add_argument("--stop-before-year", type=int, default=1560,
                    help="stop expanding ancestors born before this (default 1560, "
                         "just below the oldest gateway b.1570). Primary stopper.")
    ap.add_argument("--birth-countries", default="",
                    help="optional DESCENDANT-SIDE (Americas) allow-list to halt a "
                         "line at the ocean crossing, e.g. 'United States,Virginia,"
                         "Massachusetts,Maryland'. Do NOT add Britain. Off by default.")
    ap.add_argument("--out", default=os.path.join(DATA, "my_pedigree.csv"))
    ap.add_argument("--gedcom", help="also write a GEDCOM to this path")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds between API calls")
    ap.add_argument("--cache-dir", default=os.path.join(DATA, "cache", "ancestry"),
                    help="dir for cached ancestry responses (enables resume)")
    ap.add_argument("--no-cache", action="store_true", help="disable the disk cache")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore cached responses and refetch (updates the cache)")
    args = ap.parse_args()

    hosts = HOSTS["beta" if args.beta else "prod"]

    token = args.token
    if args.login:
        if not args.client_id:
            sys.exit("--login requires --client-id <APP_KEY>")
        token = oauth_login(args.client_id, hosts)
        print("got access token (export it as FS_ACCESS_TOKEN to reuse)")
    if not token:
        sys.exit("no token: pass --token / set FS_ACCESS_TOKEN, or use --login")

    root = args.root or current_person_id(token, hosts)
    print(f"root person: {root}")
    countries = [c for c in args.birth_countries.split(",") if c.strip()]
    cache_dir = None if args.no_cache else args.cache_dir
    people = export_pedigree(root, token, hosts, args.max_generations,
                             args.stop_before_year, countries, pause=args.pause,
                             cache_dir=cache_dir, refresh=args.refresh)

    n = write_csv(people, args.out)
    print(f"wrote {n} direct ancestors -> {args.out}")
    if args.gedcom:
        write_gedcom(people, args.gedcom)
        print(f"wrote GEDCOM -> {args.gedcom}")
    # quick depth histogram
    import collections
    hist = collections.Counter(p["generation"] for p in people.values())
    print("generations:", {g: hist[g] for g in sorted(hist)})


if __name__ == "__main__":
    main()
