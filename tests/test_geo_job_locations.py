"""ATS jobs with only a Danish city still get cached distance coordinates."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import geo
from db import Job


class _Response:
    status_code = 200

    @staticmethod
    def json():
        return [{"lat": "55.6761", "lon": "12.5683"}]


def test_danish_place_lookup_is_cached():
    cache = {}
    with mock.patch.object(geo.httpx, "get", return_value=_Response()) as request:
        first = geo.geocode_dk_place("Copenhagen", cache)
        second = geo.geocode_dk_place("Copenhagen", cache)
    assert first == second == (55.6761, 12.5683)
    assert request.call_count == 1


def test_job_without_zip_falls_back_to_danish_city():
    job = Job(
        id="ashby:demo:1", source="ashby", title="Demo",
        country="DK", city="Copenhagen",
    )
    with mock.patch.object(geo, "_load_cache", return_value={}), \
            mock.patch.object(geo, "_save_cache"), \
            mock.patch.object(geo, "geocode_dk_place", return_value=(55.6761, 12.5683)), \
            mock.patch.object(geo.time, "sleep"):
        assert geo.geocode_jobs([job]) == 1
    assert (job.lat, job.lon) == (55.6761, 12.5683)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТА ПРОШЛИ")
