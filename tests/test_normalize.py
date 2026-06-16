from vibecamp_expansion.normalize import content_hash, is_placeholder, normalize


def raw(**over):
    base = {
        "name": "Test Event",
        "description": "hello",
        "start_datetime": "2026-06-19T18:00:00.000Z",
        "end_datetime": "2026-06-19T19:30:00.000Z",
        "plaintext_location": None,
        "event_site_location": "site-1",
        "event_site_location_name": "Barn Theater",
        "event_id": "abc",
        "created_by_account_id": "acct-1",
        "event_type": "UNOFFICIAL",
        "will_be_filmed": False,
        "av_needs": None,
        "creator_name": "Joe",
        "bookmarks": 5,
    }
    base.update(over)
    return base


def test_normalize_basic_derivations():
    ev = normalize(raw())
    assert ev["start_date"] == "2026-06-19"
    assert ev["duration_minutes"] == 90
    assert ev["location"] == "Barn Theater"
    assert ev["is_placeholder"] is False
    assert ev["will_be_filmed"] is False


def test_wall_clock_not_timezone_converted():
    # The trailing Z is a lie; we keep wall-clock time.
    ev = normalize(raw(start_datetime="2026-06-19T23:30:00.000Z"))
    assert ev["start_date"] == "2026-06-19"


def test_placeholder_detection():
    assert is_placeholder(raw(start_datetime="1999-06-19T00:00:00.000Z"))
    assert is_placeholder(raw(start_datetime="3025-06-23T07:30:00.000Z"))
    assert not is_placeholder(raw(start_datetime="2026-06-19T00:00:00.000Z"))


def test_location_falls_back_to_plaintext():
    ev = normalize(raw(event_site_location_name=None, event_site_location=None,
                       plaintext_location="1300 E 4th st, Austin TX"))
    assert ev["location"] == "1300 E 4th st, Austin TX"


def test_content_hash_ignores_bookmarks():
    a = content_hash(raw(bookmarks=1))
    b = content_hash(raw(bookmarks=999))
    assert a == b
    c = content_hash(raw(name="Different"))
    assert c != a
