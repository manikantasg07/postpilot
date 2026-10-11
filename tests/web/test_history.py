"""The dashboard is a glance; the archive is its own page."""
from __future__ import annotations

from fastapi.testclient import TestClient

from agent.state import HistoryEntry, load_state, save_state
from web.app import app
from web.deps import RECENT_ON_DASHBOARD

client = TestClient(app)


def seed(count: int = 12) -> None:
    state = load_state()
    for n in range(count):
        state.add(HistoryEntry(
            date=f"2026-09-{n + 1:02d}", slot="09:00", topic=f"Topic {n}",
            status="posted" if n % 4 else "failed",
            post_id="urn:li:share:1" if n % 4 else "",
            error="" if n % 4 else "LinkedIn said no"))
    save_state(state)


def rows(body: str) -> int:
    return body.count('<td class="topic">')


def test_the_dashboard_shows_only_the_latest_few():
    seed(12)
    assert rows(client.get("/").text) == RECENT_ON_DASHBOARD


def test_the_dashboard_offers_the_rest():
    seed(12)
    body = client.get("/").text
    assert "View all 12 runs" in body
    assert "/history" in body


def test_no_link_when_five_is_all_of_them():
    """An archive link to nothing extra is a lie about how much there is."""
    seed(3)
    assert "View all" not in client.get("/").text


def test_history_shows_everything():
    seed(12)
    assert rows(client.get("/history").text) == 12


def test_history_keeps_the_failures():
    """"What happened on the 28th" is usually asked when something did not work."""
    seed(12)
    assert "LinkedIn said no" in client.get("/history").text


def test_history_is_newest_first():
    seed(12)
    body = client.get("/history").text
    assert body.index("Topic 11") < body.index("Topic 0")


def test_history_can_be_filtered():
    seed(12)
    assert rows(client.get("/history?status=failed").text) == 3
    assert rows(client.get("/history?status=posted").text) == 9


def test_an_unknown_filter_shows_nothing_rather_than_everything():
    seed(12)
    assert rows(client.get("/history?status=nonsense").text) == 0


def test_history_opens_on_a_fresh_install():
    response = client.get("/history")
    assert response.status_code == 200
    assert "Nothing has run yet" in response.text
