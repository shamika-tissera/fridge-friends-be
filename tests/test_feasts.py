import pytest

from app import notifications
from app.models import DeliveryStatus, InviteResponse

RECIPE = {
    "rank": 1,
    "rank_reason": "Uses expiring: spinach",
    "name": "Spinach Dal",
    "cuisine": "Indian",
    "uses": [
        {"name": "spinach", "from_users": ["Ann"], "expiring": True},
        {"name": "lentils", "from_users": ["Ben"], "expiring": False},
    ],
    "missing": ["cumin"],
    "uses_expiring": ["spinach"],
    "prep_minutes": 10,
    "cook_minutes": 25,
    "total_minutes": 35,
    "contributors": ["Ann", "Ben"],
    "liked_by": [],
    "is_liked": False,
    "why": "Uses up the spinach",
}


@pytest.fixture
def recording_channel(monkeypatch):
    """Capture what the channel was asked to deliver."""
    sent = []

    class Recorder:
        name = "recorder"

        def send(self, *, to, title, body):
            sent.append({"to": to.email, "title": title, "body": body})

    monkeypatch.setattr(notifications, "get_channel", lambda: Recorder())
    return sent


@pytest.fixture
def broken_channel(monkeypatch):
    class Broken:
        name = "broken"

        def send(self, *, to, title, body):
            raise RuntimeError("smtp is down")

    monkeypatch.setattr(notifications, "get_channel", lambda: Broken())


def make_feast(client, host, attendees, name="Friday Feast", **extra):
    return client.post("/feasts", json={
        "name": name, "host_id": host, "recipe": RECIPE,
        "attendee_ids": attendees, **extra,
    })


def test_creates_feast_with_recipe_snapshot(client, make_user, recording_channel):
    ann = make_user("ann@example.com", "Ann")
    ben = make_user("ben@example.com", "Ben")

    resp = make_feast(client, ann, [ben])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Friday Feast"
    assert body["host_name"] == "Ann"
    # The whole recipe is stored, not a reference to one.
    assert body["recipe"]["name"] == "Spinach Dal"
    assert body["recipe"]["total_minutes"] == 35
    assert body["recipe"]["uses"][0]["from_users"] == ["Ann"]


def test_host_is_an_attendee_and_already_accepted(client, make_user, recording_channel):
    ann = make_user("host@example.com", "Ann")
    ben = make_user("guest@example.com", "Ben")

    body = make_feast(client, ann, [ben]).json()
    by_name = {a["name"]: a for a in body["attendees"]}
    assert by_name["Ann"]["is_host"] is True
    assert by_name["Ann"]["response"] == InviteResponse.accepted
    assert by_name["Ben"]["response"] == InviteResponse.invited


def test_every_attendee_is_notified_except_the_host(client, make_user, recording_channel):
    ann = make_user("n_host@example.com", "Ann")
    ben = make_user("n_ben@example.com", "Ben")
    cal = make_user("n_cal@example.com", "Cal")

    body = make_feast(client, ann, [ben, cal]).json()
    # Delivery is a background task, so the create response reports what was
    # queued; the feast endpoint reports what actually went out.
    assert body["invitations_pending"] == 2
    assert body["invitations_sent"] == 0
    assert client.get(f"/feasts/{body['id']}").json()["invitations_sent"] == 2
    assert {s["to"] for s in recording_channel} == {"n_ben@example.com", "n_cal@example.com"}
    assert all("Ann invited you to Friday Feast" == s["title"] for s in recording_channel)

    # The host gets no invitation to their own feast.
    assert client.get(f"/users/{ann}/notifications").json() == []
    assert len(client.get(f"/users/{ben}/notifications").json()) == 1


def test_invitation_says_who_brings_what(client, make_user, recording_channel):
    ann = make_user("brings_a@example.com", "Ann")
    ben = make_user("brings_b@example.com", "Ben")
    make_feast(client, ann, [ben])

    body = recording_channel[0]["body"]
    assert "Spinach Dal" in body
    assert "Ann — spinach" in body
    assert "Ben — lentils" in body
    assert "Still to buy: cumin" in body
    assert "35 minutes" in body


def test_duplicate_and_host_ids_collapse(client, make_user, recording_channel):
    ann = make_user("dupe_a@example.com", "Ann")
    ben = make_user("dupe_b@example.com", "Ben")

    body = make_feast(client, ann, [ben, ben, ann]).json()
    assert len(body["attendees"]) == 2
    assert body["invitations_pending"] == 1
    assert client.get(f"/feasts/{body['id']}").json()["invitations_sent"] == 1


def test_notification_rows_record_delivery(client, make_user, recording_channel):
    ann = make_user("rec_a@example.com", "Ann")
    ben = make_user("rec_b@example.com", "Ben")
    make_feast(client, ann, [ben])

    note = client.get(f"/users/{ben}/notifications").json()[0]
    assert note["kind"] == "feast_invitation"
    assert note["delivery_status"] == DeliveryStatus.sent
    assert note["read_at"] is None


def test_failed_delivery_is_recorded_not_raised(client, make_user, broken_channel):
    """A dead channel must not stop the feast being created."""
    ann = make_user("fail_a@example.com", "Ann")
    ben = make_user("fail_b@example.com", "Ben")

    resp = make_feast(client, ann, [ben])
    assert resp.status_code == 201           # feast still created
    feast_id = resp.json()["id"]
    assert client.get(f"/feasts/{feast_id}").json()["invitations_sent"] == 0

    note = client.get(f"/users/{ben}/notifications").json()[0]
    assert note["delivery_status"] == DeliveryStatus.failed
    assert "smtp is down" in note["delivery_error"]


def test_resend_retries_only_undelivered(client, make_user, monkeypatch, recording_channel):
    ann = make_user("retry_a@example.com", "Ann")
    ben = make_user("retry_b@example.com", "Ben")

    class Broken:
        name = "broken"

        def send(self, **kw):
            raise RuntimeError("down")

    monkeypatch.setattr(notifications, "get_channel", lambda: Broken())
    feast = make_feast(client, ann, [ben]).json()
    assert client.get(f"/feasts/{feast['id']}").json()["invitations_sent"] == 0

    # Channel recovers; the retry delivers the outstanding invitation.
    sent = []

    class Working:
        name = "working"

        def send(self, *, to, title, body):
            sent.append(to.email)

    monkeypatch.setattr(notifications, "get_channel", lambda: Working())
    again = client.post(f"/feasts/{feast['id']}/resend-invitations").json()
    assert again["invitations_sent"] == 1
    assert sent == ["retry_b@example.com"]

    # Running it once more sends nothing new — already-sent rows are skipped.
    sent.clear()
    client.post(f"/feasts/{feast['id']}/resend-invitations")
    assert sent == []


def test_accept_and_decline(client, make_user, recording_channel):
    ann = make_user("resp_a@example.com", "Ann")
    ben = make_user("resp_b@example.com", "Ben")
    feast = make_feast(client, ann, [ben]).json()

    body = client.post(f"/feasts/{feast['id']}/respond/{ben}",
                       json={"response": "accepted"}).json()
    ben_row = next(a for a in body["attendees"] if a["user_id"] == ben)
    assert ben_row["response"] == InviteResponse.accepted
    assert ben_row["responded_at"] is not None

    body = client.post(f"/feasts/{feast['id']}/respond/{ben}",
                       json={"response": "declined"}).json()
    assert next(a for a in body["attendees"] if a["user_id"] == ben)["response"] == "declined"


def test_uninvited_user_cannot_respond(client, make_user, recording_channel):
    ann = make_user("un_a@example.com", "Ann")
    ben = make_user("un_b@example.com", "Ben")
    cal = make_user("un_c@example.com", "Cal")
    feast = make_feast(client, ann, [ben]).json()

    resp = client.post(f"/feasts/{feast['id']}/respond/{cal}", json={"response": "accepted"})
    assert resp.status_code == 404


def test_list_feasts_for_user(client, make_user, recording_channel):
    ann = make_user("list_a@example.com", "Ann")
    ben = make_user("list_b@example.com", "Ben")
    make_feast(client, ann, [ben], name="One")
    make_feast(client, ben, [], name="Two")

    ben_feasts = [f["name"] for f in client.get(f"/users/{ben}/feasts").json()]
    assert sorted(ben_feasts) == ["One", "Two"]
    hosting = [f["name"] for f in client.get(f"/users/{ben}/feasts?hosting_only=true").json()]
    assert hosting == ["Two"]


def test_unknown_host_or_attendee_is_404(client, make_user, recording_channel):
    ann = make_user("known_h@example.com", "Ann")
    assert make_feast(client, 9999, [ann]).status_code == 404
    assert make_feast(client, ann, [9999]).status_code == 404


def test_mark_notification_read(client, make_user, recording_channel):
    ann = make_user("read_a@example.com", "Ann")
    ben = make_user("read_b@example.com", "Ben")
    make_feast(client, ann, [ben])

    note = client.get(f"/users/{ben}/notifications").json()[0]
    assert len(client.get(f"/users/{ben}/notifications?unread_only=true").json()) == 1

    marked = client.post(f"/notifications/{note['id']}/read").json()
    assert marked["read_at"] is not None
    assert client.get(f"/users/{ben}/notifications?unread_only=true").json() == []


def test_deleting_a_user_removes_their_feasts_and_notifications(
    client, make_user, recording_channel, session
):
    from sqlmodel import select
    from app.models import Feast, FeastAttendee, Notification

    ann = make_user("cas_a@example.com", "Ann")
    ben = make_user("cas_b@example.com", "Ben")
    make_feast(client, ann, [ben])

    assert client.delete(f"/users/{ann}").status_code == 204
    assert session.exec(select(Feast)).all() == []
    assert session.exec(select(FeastAttendee)).all() == []
    assert session.exec(select(Notification)).all() == []


def test_scheduled_time_appears_in_the_invitation(client, make_user, recording_channel):
    ann = make_user("when_a@example.com", "Ann")
    ben = make_user("when_b@example.com", "Ben")
    make_feast(client, ann, [ben], scheduled_for="2026-10-02T19:30:00Z")

    assert "When:" in recording_channel[0]["body"]
    assert "19:30" in recording_channel[0]["body"]
