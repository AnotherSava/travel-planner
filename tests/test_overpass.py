"""Every case here is a failure mode observed live against the public Overpass API.

They all arrive as HTTP 200 with a well-formed JSON body, which is why they need
tests at all: nothing in the transport layer distinguishes them from success.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from travel_planner.sources.overpass import (
    OverpassEmptyError,
    OverpassError,
    OverpassStaleError,
    validate,
)


def payload(*, elements=None, age_days: float = 0.0, **extra) -> dict:
    stamp = datetime.now(UTC) - timedelta(days=age_days)
    return {
        "version": 0.6,
        "osm3s": {"timestamp_osm_base": stamp.isoformat().replace("+00:00", "Z")},
        "elements": [{"type": "node", "id": 1}] if elements is None else elements,
        **extra,
    }


def test_accepts_a_healthy_response():
    result = validate(payload())
    assert len(result.elements) == 1
    assert result.age < timedelta(minutes=1)


def test_rejects_timeout_disguised_as_success():
    # HTTP 200, valid JSON, empty elements, failure visible only in `remark`.
    body = payload(
        elements=[],
        remark='runtime error: Query timed out in "query" at line 4 after 4 seconds',
    )
    with pytest.raises(OverpassError, match="timed out"):
        validate(body)


def test_rejects_partial_results_carrying_a_remark():
    # The dangerous case: plausible-looking data that is silently incomplete.
    body = payload(
        elements=[{"type": "node", "id": n} for n in range(50)],
        remark="runtime error: Query run out of memory",
    )
    with pytest.raises(OverpassError, match="out of memory"):
        validate(body)


def test_rejects_stale_mirror():
    # A mirror served extracts 5 and 7 weeks old on consecutive calls.
    with pytest.raises(OverpassStaleError, match="stale"):
        validate(payload(age_days=40))


def test_accepts_extract_within_freshness_window():
    assert validate(payload(age_days=3)).elements


def test_rejects_empty_result_by_default():
    # An area id derived from a node returns zero elements with no error at all.
    with pytest.raises(OverpassEmptyError):
        validate(payload(elements=[]))


def test_allows_empty_when_explicitly_requested():
    assert validate(payload(elements=[]), allow_empty=True).elements == []


def test_rejects_response_without_provenance():
    body = payload()
    del body["osm3s"]
    with pytest.raises(OverpassError, match="provenance"):
        validate(body)


def test_rejects_response_without_elements_key():
    body = payload()
    del body["elements"]
    with pytest.raises(OverpassError, match="no 'elements' key"):
        validate(body)
