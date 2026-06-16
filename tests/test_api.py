import pytest
from fastapi.testclient import TestClient

from vibecamp_expansion import api
from vibecamp_expansion.normalize import normalize
from vibecamp_expansion.store import Store


def raw(eid, **over):
    base = {
        "name": f"Event {eid}", "description": "desc",
        "start_datetime": "2026-06-19T18:00:00.000Z",
        "end_datetime": "2026-06-19T19:00:00.000Z",
        "plaintext_location": None, "event_site_location": "site-1",
        "event_site_location_name": "Barn Theater", "event_id": eid,
        "created_by_account_id": "acct-1", "event_type": "UNOFFICIAL",
        "will_be_filmed": False, "av_needs": None, "creator_name": "Joe",
        "bookmarks": 0,
    }
    base.update(over)
    return base


@pytest.fixture
def client(tmp_path):
    store = Store(db_path=tmp_path / "api.db")
    raws = [raw("a", name="Emo Night", bookmarks=10),
            raw("b", name="Yoga", event_type="TEAM_OFFICIAL")]
    store.reconcile([normalize(r) for r in raws], {r["event_id"]: r for r in raws})
    api.set_store(store)
    return TestClient(api.app)


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_list_events(client):
    data = client.get("/events").json()
    assert data["total"] == 2
    assert len(data["events"]) == 2


def test_search(client):
    data = client.get("/events", params={"q": "emo"}).json()
    assert data["total"] == 1
    assert data["events"][0]["name"] == "Emo Night"


def test_filter_type(client):
    data = client.get("/events", params={"type": "TEAM_OFFICIAL"}).json()
    assert data["total"] == 1
    assert data["events"][0]["name"] == "Yoga"


def test_get_event_and_404(client):
    assert client.get("/events/a").json()["event_id"] == "a"
    assert client.get("/events/nope").status_code == 404


def test_days_and_sites(client):
    days = client.get("/days").json()
    assert days[0]["date"] == "2026-06-19"
    assert days[0]["event_count"] == 2
    sites = client.get("/sites").json()
    assert sites[0]["event_site_location_name"] == "Barn Theater"


def test_stats(client):
    s = client.get("/stats").json()
    assert s["active_events"] == 2
    assert s["by_type"]["TEAM_OFFICIAL"] == 1


def test_history_endpoint(client):
    h = client.get("/history").json()
    assert h["total"] == 2  # two creates
    assert all(c["change_type"] == "created" for c in h["history"])


def test_openapi_available(client):
    spec = client.get("/openapi.json").json()
    assert "/events" in spec["paths"]
