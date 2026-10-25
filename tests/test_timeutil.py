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


def test_a_resolved_zone_answers_like_its_name():
    stamp = "2026-07-15T23:30:00+00:00"
    assert local_parts(stamp, resolve_tz("Europe/London")) == local_parts(stamp, "Europe/London")

def test_auto_resolves_to_the_host_zone():
    tz = resolve_tz("auto")
    assert datetime.now(tz).utcoffset() == datetime.now().astimezone().utcoffset()


def test_the_two_stamp_formats_stay_what_their_readers_expect():
    from discord_scraper.timeutil import precise_stamp, utc_stamp

    # Control-plane rows: UTC, whole seconds.
    plain = datetime.fromisoformat(utc_stamp())
    assert plain.utcoffset().total_seconds() == 0 and plain.microsecond == 0
    # Rebuild stamps: microseconds, so two rebuilds in one second stay distinct.
    precise = precise_stamp()
    assert datetime.fromisoformat(precise).tzinfo is not None
    assert len(precise.split(".")[1].split("+")[0].split("-")[0]) == 6
