"""Unit tests for the pure parsing + cutoff logic of fs_export (no network).

Run:  python3 tests/test_fs_export.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import fs_export as fx  # noqa: E402

# A minimal GEDCOM X ancestry response: root + parents + one grandparent.
ANCESTRY = {
    "persons": [
        {"id": "ROOT", "display": {"name": "Me Mine", "ascendancyNumber": "1",
                                   "birthDate": "1989", "birthPlace": "Boston, Massachusetts, United States"},
         "names": [{"nameForms": [{"parts": [
             {"type": "http://gedcomx.org/Given", "value": "Me"},
             {"type": "http://gedcomx.org/Surname", "value": "Mine"}]}]}]},
        {"id": "DAD", "display": {"name": "John Mine", "ascendancyNumber": "2",
                                  "birthDate": "1960", "birthPlace": "Boston, Massachusetts, United States"}},
        {"id": "MOM", "display": {"name": "Jane Abell", "ascendancyNumber": "3",
                                  "birthDate": "1962", "birthPlace": "Providence, Rhode Island, United States"}},
        # great-great...-grandparent born too early to keep expanding
        {"id": "OLD", "display": {"name": "Ancient One", "ascendancyNumber": "128",
                                  "birthDate": "1450", "birthPlace": "Cologne, Germany"}},
    ]
}


def test_parse_generations_and_fields():
    people = fx.parse_ancestry(ANCESTRY)
    assert people["ROOT"]["generation"] == 0
    assert people["DAD"]["generation"] == 1
    assert people["MOM"]["generation"] == 1
    assert people["OLD"]["generation"] == 7  # 128 -> bit_length 8 -> gen 7
    assert people["ROOT"]["surname"] == "Mine"
    assert people["MOM"]["surname"] == "Abell"  # split from display name
    assert people["DAD"]["birth_year"] == "1960"
    assert people["ROOT"]["birth_country"] == "United States"
    print("ok: parse_ancestry generations + fields")


def test_gen_offset_for_rerooting():
    people = fx.parse_ancestry(ANCESTRY, gen_offset=8)
    assert people["ROOT"]["generation"] == 8
    assert people["DAD"]["generation"] == 9
    print("ok: gen_offset re-rooting")


def test_cutoffs():
    p_root = fx.parse_ancestry(ANCESTRY)["ROOT"]
    p_old = fx.parse_ancestry(ANCESTRY)["OLD"]
    p_mom = fx.parse_ancestry(ANCESTRY)["MOM"]
    # depth cap
    assert fx.should_expand(p_root, max_generations=0, stop_before_year=1500,
                            birth_countries=[]) is False
    assert fx.should_expand(p_root, 15, 1500, []) is True
    # born before the floor -> do not expand
    assert fx.should_expand(p_old, 15, 1500, []) is False
    # country allow-list: US ok, Germany pruned
    assert fx.should_expand(p_mom, 15, 1500, ["United States", "England"]) is True
    assert fx.should_expand(p_old, 15, 1000, ["United States", "England"]) is False
    # missing data is never pruned
    p_nodate = {"generation": 3, "birth_year": "", "birth_place": ""}
    assert fx.should_expand(p_nodate, 15, 1500, ["England"]) is True
    print("ok: cutoffs (depth, year floor, country, missing-data)")


def test_csv_roundtrip(tmp="/tmp/_ped_test.csv"):
    people = fx.parse_ancestry(ANCESTRY)
    n = fx.write_csv(people, tmp)
    assert n == 4
    with open(tmp) as fh:
        head = fh.readline().strip()
    assert head.startswith("id,generation,full_name")
    print("ok: write_csv")


def test_retry_after_header():
    class E:  # minimal stand-in for urllib HTTPError (.headers.get)
        def __init__(self, h): self.headers = h
    assert fx._retry_after(E({"Retry-After": "30"})) == 30.0
    assert fx._retry_after(E({})) == 0.0
    assert fx._retry_after(E(None)) == 0.0
    assert fx._retry_after(E({"Retry-After": "not-a-number"})) == 0.0
    # an HTTP-date a minute out -> roughly 60s (allow slack)
    from email.utils import format_datetime
    from datetime import datetime, timezone, timedelta
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60))
    assert 50 <= fx._retry_after(E({"Retry-After": future})) <= 61
    print("ok: Retry-After parsing (seconds, date, missing, junk)")


def test_cache_roundtrip(tmp="/tmp/_cache_test"):
    fx.cache_write(tmp, "ABC-1", {"persons": [{"id": "X"}]})
    assert fx.cache_read(tmp, "ABC-1") == {"persons": [{"id": "X"}]}
    assert fx.cache_read(tmp, "MISSING-9") is None
    print("ok: cache read/write roundtrip")


def test_fetch_serves_from_cache_without_network(tmp="/tmp/_cache_test2"):
    fx.cache_write(tmp, "ROOT", ANCESTRY)
    orig = fx.http_get
    fx.http_get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used!"))
    try:
        data, from_cache = fx.fetch_ancestry("ROOT", token=None, hosts=fx.HOSTS["prod"],
                                             cache_dir=tmp)
        assert from_cache is True and data == ANCESTRY
    finally:
        fx.http_get = orig
    print("ok: fetch_ancestry serves cache hit without network")


def test_export_resumes_from_cache(tmp="/tmp/_cache_test3"):
    # Pre-seed the cache for ROOT; its deepest ancestor (b.1450) is pruned by the
    # year floor, so no further calls are needed -> the whole export runs offline.
    fx.cache_write(tmp, "ROOT", ANCESTRY)
    orig = fx.http_get
    fx.http_get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used!"))
    try:
        people = fx.export_pedigree("ROOT", token=None, hosts=fx.HOSTS["prod"],
                                    max_generations=15, stop_before_year=1500,
                                    birth_countries=[], pause=0, cache_dir=tmp)
        assert set(people) == {"ROOT", "DAD", "MOM", "OLD"}
    finally:
        fx.http_get = orig
    print("ok: export_pedigree resumes entirely from cache (no network)")


if __name__ == "__main__":
    test_parse_generations_and_fields()
    test_gen_offset_for_rerooting()
    test_cutoffs()
    test_csv_roundtrip()
    test_retry_after_header()
    test_cache_roundtrip()
    test_fetch_serves_from_cache_without_network()
    test_export_resumes_from_cache()
    print("\nALL TESTS PASSED")
