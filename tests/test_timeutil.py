from datetime import datetime

from discord_scraper.timeutil import local_parts, resolve_tz


def test_dst_boundary_shifts_the_local_day():
    # 23:30 UTC in July is 00:30 the NEXT day in London (BST, UTC+1)
    d, y, dow, h = local_parts("2026-07-15T23:30:00+00:00", "Europe/London")
    assert d == "2026-07-16" and h == 0
    # the same clock time in January is still the same day (GMT, UTC+0)
    d2, _, _, h2 = local_parts("2026-01-15T23:30:00+00:00", "Europe/London")
    assert d2 == "2026-01-15" and h2 == 23


def test_utc_default_is_identity():
    assert local_parts("2026-07-15T23:30:00+00:00", "UTC")[0] == "2026-07-15"


def test_auto_resolves_to_the_host_zone():
    tz = resolve_tz("auto")
    assert datetime.now(tz).utcoffset() == datetime.now().astimezone().utcoffset()
