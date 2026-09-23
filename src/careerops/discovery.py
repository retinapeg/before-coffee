"""The two names the digest and the refresh loop read from the collector, written as a
stand-in for this public extract.

The private discovery module (board registry, fetching, parsing, deduplication) is
much larger and is not published. This stand-in differs from it as follows:

- Only `COVERAGE_DEFAULTS` and `_queries` exist. `COVERAGE_DEFAULTS` is the private
  table of generic integer limits, unchanged.
- `_queries` keeps the private logic but has no built-in role families. The private
  version falls back to the owner's own search terms, which are not published, so
  here an unconfigured search yields no queries.
"""
from __future__ import annotations

COVERAGE_DEFAULTS = {
    "query_objective": 50, "board_objective": 100, "max_requests": 1000,
    "max_new_employer_requests": 150, "max_board_requests": 400,
    "max_vacancy_requests": 400, "max_historical_requests": 0,
    "max_ai_reviews": 25, "per_host_limit": 200, "timeout_seconds": 900,
}


def _queries(settings, maximum):
    search = settings.get("search", {})
    explicit = search.get("web", {}).get("queries") or []
    if explicit:
        return [str(q)[:350] for q in explicit[:maximum] if q]
    roles = search.get("role_families") or []
    locations = []
    for country, config in settings.get("locations", {}).items():
        if isinstance(config, dict) and config.get("enabled", True):
            locations.extend(config.get("cities") or [country])
    if settings.get("lanes", {}).get("london", True):
        locations.append("London")
    return [f'{role} jobs {location}' for role in roles for location in locations][:maximum]
