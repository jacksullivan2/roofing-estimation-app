"""Loader for the Roofing Estimation Question Map.

The map is the data contract for the 'Add context' UI: groups -> sub-elements
-> questions (with rich metadata). It is generated from the Question Map
workbook and shipped as app/data/question_map.json. Loaded once at import.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DATA_PATH = Path(__file__).parent / "data" / "question_map.json"


@lru_cache(maxsize=1)
def load() -> dict:
    with open(_DATA_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def groups() -> list[dict]:
    return load().get("groups", [])


def all_questions() -> list[dict]:
    out = []
    for g in groups():
        for sub in g.get("subelements", []):
            out.extend(sub.get("questions", []))
    return out


def question_index() -> dict[str, dict]:
    """qid -> question dict (with group/subelement names attached)."""
    idx: dict[str, dict] = {}
    for g in groups():
        for sub in g.get("subelements", []):
            for q in sub.get("questions", []):
                idx[q["qid"]] = {**q, "group": g["group"], "subelement": sub["name"]}
    return idx


def total_questions() -> int:
    return load().get("n_questions", len(all_questions()))


def valid_qids() -> set[str]:
    return set(question_index().keys())
