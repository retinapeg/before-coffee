"""A minimal location classifier, written as a stand-in for this public extract.

The private `classify_job` is much larger and is not published. This stand-in
answers only what `digest._region` asks, and differs from the private one as follows:

- It reads the job's `location` for configured city names and the word "London"
  only (whole words, case insensitive). The one other input is a two-letter ISO
  code in the job's `country` field, used as given.
- Country names are not resolved: "Paris, France" counts as FR only because "Paris"
  is a configured city for FR. There is no built-in city table.
- `london_alternative` is always False.
- ATS `available_locations`, remote-country lists, excluded cities, mentions of
  headquarters or customers, and the private routing modes are not supported.
- "New London, CT" is classified as London.
"""
from __future__ import annotations

import re


def _mentions(text: str, name: str) -> bool:
    return bool(name) and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I) is not None


def classify_job(job: dict, settings: dict) -> dict:
    location = str(job.get("location") or "")
    london = _mentions(location, "London")
    countries = set()
    declared = str(job.get("country") or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}", declared):
        countries.add(declared)
    if london:
        countries.add("GB")
    for code, preference in (settings.get("locations") or {}).items():
        cities = (preference.get("cities") or []) if isinstance(preference, dict) else []
        if any(_mentions(location, str(city)) for city in cities):
            countries.add(str(code).upper())
    overseas = sorted(countries - {"GB"})
    region = "london" if london else "overseas" if overseas else "other"
    return {"region": region, "london_alternative": False, "all_countries": sorted(countries)}
