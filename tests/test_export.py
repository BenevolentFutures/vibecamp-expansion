import json

from vibecamp_expansion.export import generate_exports
from vibecamp_expansion.normalize import normalize
from vibecamp_expansion.store import Store


def raw(eid, **over):
    base = {
        "name": f"Event {eid}", "description": "desc here",
        "start_datetime": "2026-06-19T18:00:00.000Z",
        "end_datetime": "2026-06-19T19:00:00.000Z",
        "plaintext_location": None, "event_site_location": "site-1",
        "event_site_location_name": "Barn Theater", "event_id": eid,
        "created_by_account_id": "acct-1", "event_type": "UNOFFICIAL",
        "will_be_filmed": False, "av_needs": None, "creator_name": "Joe",
        "bookmarks": 7,
    }
    base.update(over)
    return base


def test_generate_exports_writes_all_files(tmp_path):
    store = Store(db_path=tmp_path / "e.db")
    raws = [
        raw("a", name="Sea Shanty Sunset", bookmarks=99),
        raw("old", start_datetime="2025-06-21T18:00:00.000Z"),  # past edition
    ]
    store.reconcile([normalize(r) for r in raws], {r["event_id"]: r for r in raws})

    out = tmp_path / "public"
    manifest = generate_exports(store, out)

    # current edition has 1 event; all editions has 2
    assert manifest["current_edition_count"] == 1
    assert manifest["all_editions_count"] == 2

    for name in ("events.json", "events.ndjson", "events.csv", "schedule.md",
                 "events.ics", "llms.txt", "index.json", "events.all.ndjson"):
        assert (out / name).exists(), name

    # ndjson: one line per current event, each valid JSON with stars==bookmarks
    lines = (out / "events.ndjson").read_text().strip().splitlines()
    assert len(lines) == 1
    ev = json.loads(lines[0])
    assert ev["stars"] == ev["bookmarks"] == 99

    # all-editions ndjson has both
    assert len((out / "events.all.ndjson").read_text().strip().splitlines()) == 2

    # schedule.md is greppable by name
    assert "Sea Shanty Sunset" in (out / "schedule.md").read_text()

    # ics is well-formed enough
    ics = (out / "events.ics").read_text()
    assert ics.startswith("BEGIN:VCALENDAR")
    assert "BEGIN:VEVENT" in ics
