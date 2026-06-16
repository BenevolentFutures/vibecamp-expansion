import pytest

from vibecamp_expansion.normalize import normalize
from vibecamp_expansion.store import Store


def raw(eid, **over):
    base = {
        "name": f"Event {eid}",
        "description": "desc",
        "start_datetime": "2026-06-19T18:00:00.000Z",
        "end_datetime": "2026-06-19T19:00:00.000Z",
        "plaintext_location": None,
        "event_site_location": "site-1",
        "event_site_location_name": "Barn Theater",
        "event_id": eid,
        "created_by_account_id": "acct-1",
        "event_type": "UNOFFICIAL",
        "will_be_filmed": False,
        "av_needs": None,
        "creator_name": "Joe",
        "bookmarks": 0,
    }
    base.update(over)
    return base


def reconcile(store, raws):
    by_id = {r["event_id"]: r for r in raws}
    normd = [normalize(r) for r in raws]
    return store.reconcile(normd, by_id)


@pytest.fixture
def store(tmp_path):
    return Store(db_path=tmp_path / "test.db")


def test_create(store):
    counts = reconcile(store, [raw("a"), raw("b")])
    assert counts["created"] == 2
    events, total = store.query_events()
    assert total == 2


def test_update_records_history(store):
    reconcile(store, [raw("a")])
    counts = reconcile(store, [raw("a", name="Renamed")])
    assert counts["updated"] == 1
    ev = store.get_event("a")
    assert ev["name"] == "Renamed"
    assert ev["revision"] == 1
    hist, total = store.history(event_id="a")
    assert {h["change_type"] for h in hist} == {"created", "updated"}
    upd = [h for h in hist if h["change_type"] == "updated"][0]
    assert any(c["field"] == "name" for c in upd["changes"])


def test_bookmarks_do_not_create_history(store):
    reconcile(store, [raw("a", bookmarks=0)])
    counts = reconcile(store, [raw("a", bookmarks=50)])
    assert counts["unchanged"] == 1
    assert counts["updated"] == 0
    assert store.get_event("a")["bookmarks"] == 50
    hist, total = store.history(event_id="a")
    assert total == 1  # only the create


def test_soft_delete(store):
    reconcile(store, [raw("a"), raw("b")])
    counts = reconcile(store, [raw("a")])  # b vanished
    assert counts["deleted"] == 1
    # default query hides deleted
    events, total = store.query_events()
    assert total == 1
    # but it's still there with include_deleted
    events, total = store.query_events(include_deleted=True)
    assert total == 2
    b = store.get_event("b")
    assert b["is_deleted"] is True
    assert b["deleted_at"] is not None


def test_resurrect(store):
    reconcile(store, [raw("a")])
    reconcile(store, [])  # a deleted
    counts = reconcile(store, [raw("a")])  # a returns
    assert counts["resurrected"] == 1
    a = store.get_event("a")
    assert a["is_deleted"] is False
    assert a["deleted_at"] is None
    hist, total = store.history(event_id="a")
    assert {h["change_type"] for h in hist} == {"created", "deleted", "resurrected"}


def test_fts_search(store):
    reconcile(store, [raw("a", name="Emo Night"), raw("b", name="Yoga Session")])
    events, total = store.query_events(q="emo")
    assert total == 1 and events[0]["event_id"] == "a"
    events, total = store.query_events(q="yog")  # prefix match
    assert total == 1 and events[0]["event_id"] == "b"


def test_deleted_event_removed_from_fts(store):
    reconcile(store, [raw("a", name="Findme")])
    events, total = store.query_events(q="findme")
    assert total == 1
    reconcile(store, [])  # delete
    events, total = store.query_events(q="findme")
    assert total == 0


def test_placeholder_filtered_by_default(store):
    reconcile(store, [raw("a"), raw("z", start_datetime="3025-01-01T00:00:00.000Z")])
    events, total = store.query_events()
    assert total == 1
    events, total = store.query_events(include_placeholder=True)
    assert total == 2


def test_filters(store):
    reconcile(store, [
        raw("a", event_type="TEAM_OFFICIAL", will_be_filmed=True, bookmarks=10),
        raw("b", event_type="UNOFFICIAL", creator_name="Alice"),
    ])
    assert store.query_events(event_type="TEAM_OFFICIAL")[1] == 1
    assert store.query_events(filmed=True)[1] == 1
    assert store.query_events(creator="Alice")[1] == 1
    assert store.query_events(min_bookmarks=5)[1] == 1
    assert store.query_events(day="2026-06-19")[1] == 2


def test_stats_and_crawl_log(store):
    cid = store.start_crawl()
    reconcile(store, [raw("a"), raw("z", start_datetime="3025-01-01T00:00:00.000Z")])
    store.finish_crawl(cid, status="ok", http_status=200, event_count=2,
                       counts={"created": 2})
    s = store.stats()
    assert s["active_events"] == 2
    assert s["placeholder_events"] == 1
    assert s["crawl"]["last_status"] == "ok"
    assert s["crawl"]["total_crawls"] == 1
