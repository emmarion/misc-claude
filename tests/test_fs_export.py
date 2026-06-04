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


if __name__ == "__main__":
    test_parse_generations_and_fields()
    test_gen_offset_for_rerooting()
    test_cutoffs()
    test_csv_roundtrip()
    print("\nALL TESTS PASSED")
