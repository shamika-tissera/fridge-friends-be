"""The You screen: account, friends, and waste-and-spending."""

from datetime import date, timedelta

TODAY = date.today()


def iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def add(client, uid, name, days=None, price=None, bought=0):
    body = {"name": name, "price": price, "purchased_on": iso(-bought)}
    if days is not None:
        body["expires_on"] = iso(days)
    r = client.post(f"/users/{uid}/grocery-items", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------- Account ----------
def test_user_id_can_be_edited(client, make_user):
    uid = make_user("edit@example.com", username="old.handle")
    resp = client.patch(f"/users/{uid}", json={"username": "New.Handle"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == "new.handle"
    # The old handle stops working and the new one takes over.
    assert client.get("/users?username=old.handle").json() == []
    assert client.get("/users?username=new.handle").json()[0]["id"] == uid


def test_editing_to_a_taken_user_id_is_rejected(client, make_user):
    make_user("first@example.com", username="taken.id")
    uid = make_user("second@example.com", username="mine.id")
    resp = client.patch(f"/users/{uid}", json={"username": "taken.id"})
    assert resp.status_code == 409
    assert "User ID" in resp.json()["detail"]


def test_invalid_user_id_is_rejected(client, make_user):
    uid = make_user("bad@example.com")
    assert client.patch(f"/users/{uid}", json={"username": "has spaces"}).status_code == 422


# ---------- Friends ----------
def test_invite_by_user_id(client, make_user):
    me = make_user("inviter@example.com", username="me.here")
    them = make_user("invitee@example.com", username="maya.cooks")

    resp = client.post(f"/users/{me}/friends/invite", json={"username": "Maya.Cooks"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["friend_id"] == them
    assert resp.json()["status"] == "pending"

    # Inviting again is a conflict, not a second row.
    assert client.post(f"/users/{me}/friends/invite", json={"username": "maya.cooks"}).status_code == 409


def test_invite_unknown_user_id(client, make_user):
    me = make_user("lonely@example.com")
    resp = client.post(f"/users/{me}/friends/invite", json={"username": "ghost.user"})
    assert resp.status_code == 404
    assert "ghost.user" in resp.json()["detail"]


def test_friends_list_carries_rescue_counts(client, make_user):
    me = make_user("counter@example.com", username="me.counts")
    maya = make_user("maya@example.com", "Maya", username="maya.x")

    add(client, maya, "Spinach", days=1)      # use_now
    add(client, maya, "Eggplant", days=-1)    # expired
    add(client, maya, "Milk", days=4)         # use_soon, not a rescue
    add(client, maya, "Rice", days=300)       # fresh

    client.post(f"/users/{me}/friends/invite", json={"username": "maya.x"})
    client.patch(f"/users/{me}/friends/{maya}", json={"status": "accepted"})

    row = client.get(f"/users/{me}/friends").json()[0]
    assert row["friend"]["name"] == "Maya"
    assert row["needs_rescue"] == 2


def test_friend_shelf_shows_yellow_and_red_only_and_hides_prices(client, make_user):
    me = make_user("viewer@example.com")
    pal = make_user("shelfowner@example.com", "Pal", username="pal.x")

    add(client, pal, "Spinach", days=1, price=3.49)    # red
    add(client, pal, "Milk", days=4, price=2.00)       # yellow
    add(client, pal, "Rice", days=300, price=8.00)     # green — not shared

    client.post(f"/users/{me}/friends/invite", json={"username": "pal.x"})
    client.patch(f"/users/{me}/friends/{pal}", json={"status": "accepted"})

    body = client.get(f"/users/{me}/friends/{pal}/shelf").json()
    assert [i["name"] for i in body["items"]] == ["Spinach", "Milk"]
    assert body["needs_rescue"] == 1 and body["use_soon"] == 1
    # The promise on the You screen: prices stay private.
    assert all("price" not in item for item in body["items"])


def test_friend_shelf_needs_an_accepted_friendship(client, make_user):
    me = make_user("nosy@example.com")
    them = make_user("private@example.com", username="them.x")
    add(client, them, "Spinach", days=1, price=3.49)

    # No friendship at all.
    assert client.get(f"/users/{me}/friends/{them}/shelf").status_code == 403

    # Still only a pending request.
    client.post(f"/users/{me}/friends/invite", json={"username": "them.x"})
    assert client.get(f"/users/{me}/friends/{them}/shelf").status_code == 403

    client.patch(f"/users/{me}/friends/{them}", json={"status": "accepted"})
    assert client.get(f"/users/{me}/friends/{them}/shelf").status_code == 200


# ---------- resolving items ----------
def test_using_an_item_in_the_red_counts_as_a_rescue(client, make_user):
    uid = make_user("rescuer@example.com")
    item = add(client, uid, "Spinach", days=1, price=3.49)

    body = client.post(f"/grocery-items/{item['id']}/resolve", json={"outcome": "used"}).json()
    assert body["outcome"] == "used"
    assert body["rescued"] is True
    assert body["consumed"] is True
    assert body["resolved_on"] == TODAY.isoformat()


def test_using_a_fresh_item_is_not_a_rescue(client, make_user):
    uid = make_user("earlybird@example.com")
    item = add(client, uid, "Rice", days=300, price=8.00)
    body = client.post(f"/grocery-items/{item['id']}/resolve", json={"outcome": "used"}).json()
    assert body["rescued"] is False


def test_wasting_an_item(client, make_user):
    uid = make_user("waster@example.com")
    item = add(client, uid, "Spinach", days=-2, price=3.49)
    body = client.post(f"/grocery-items/{item['id']}/resolve", json={"outcome": "wasted"}).json()
    assert body["outcome"] == "wasted" and body["rescued"] is False
    assert client.get(f"/users/{uid}/shelf").json()["items"] == []


def test_on_shelf_is_not_a_resolution(client, make_user):
    uid = make_user("noop@example.com")
    item = add(client, uid, "Spinach", days=1)
    assert client.post(f"/grocery-items/{item['id']}/resolve",
                       json={"outcome": "on_shelf"}).status_code == 422


def test_legacy_consumed_flag_still_sets_an_outcome(client, make_user):
    uid = make_user("legacy@example.com")
    item = add(client, uid, "Spinach", days=1, price=3.49)

    body = client.patch(f"/grocery-items/{item['id']}", json={"consumed": True}).json()
    assert body["outcome"] == "used" and body["rescued"] is True

    # Putting it back clears the record of how it ended.
    back = client.patch(f"/grocery-items/{item['id']}", json={"consumed": False}).json()
    assert back["outcome"] == "on_shelf" and back["resolved_on"] is None
    assert back["rescued"] is False


# ---------- waste and spending ----------
def test_stats_totals_and_trend(client, make_user):
    uid = make_user("stats@example.com")

    # Bought five weeks ago, binned five weeks ago: early waste.
    old = add(client, uid, "Old spinach", days=-30, price=10.00, bought=35)
    client.post(f"/grocery-items/{old['id']}/resolve",
                json={"outcome": "wasted", "resolved_on": iso(-35)})
    kept = add(client, uid, "Old rice", days=300, price=40.00, bought=35)
    client.post(f"/grocery-items/{kept['id']}/resolve",
                json={"outcome": "used", "resolved_on": iso(-35)})

    # Bought this week, rescued this week: no waste.
    now = add(client, uid, "Fresh spinach", days=1, price=5.00, bought=0)
    client.post(f"/grocery-items/{now['id']}/resolve", json={"outcome": "used"})

    body = client.get(f"/users/{uid}/stats?period=weeks&buckets=6").json()
    assert [b["label"] for b in body["buckets"]] == ["W1", "W2", "W3", "W4", "W5", "W6"]
    assert body["spent"] == 55.00
    assert body["wasted"] == 10.00
    assert body["rescued"] == 5.00

    first, last = body["buckets"][0], body["buckets"][-1]
    assert first["spent"] == 50.00 and first["wasted"] == 10.00
    assert first["spent_and_used"] == 40.00       # the green part of the bar
    assert last["spent"] == 5.00 and last["wasted"] == 0.0
    assert body["waste_percent_first"] == 20.0 and body["waste_percent_last"] == 0.0
    assert body["summary"] == "Waste is down from 20% of spending to 0% over 6 weeks."


def test_stats_attributes_spend_and_waste_to_different_buckets(client, make_user):
    """Bought in one week, binned in another — each lands where it happened."""
    uid = make_user("split@example.com")
    item = add(client, uid, "Milk", days=-10, price=4.00, bought=21)
    client.post(f"/grocery-items/{item['id']}/resolve",
                json={"outcome": "wasted", "resolved_on": iso(-7)})

    buckets = client.get(f"/users/{uid}/stats").json()["buckets"]
    spent_in = [b["label"] for b in buckets if b["spent"]]
    wasted_in = [b["label"] for b in buckets if b["wasted"]]
    assert spent_in and wasted_in and spent_in != wasted_in


def test_unpriced_items_are_reported_not_counted_as_free(client, make_user):
    uid = make_user("noprice@example.com")
    add(client, uid, "Spinach", days=3)           # no price
    add(client, uid, "Milk", days=3, price=2.50)

    body = client.get(f"/users/{uid}/stats").json()
    assert body["spent"] == 2.50
    assert body["priced_items"] == 1 and body["unpriced_items"] == 1


def test_stats_by_month(client, make_user):
    uid = make_user("monthly@example.com")
    add(client, uid, "Rice", days=300, price=12.00)
    body = client.get(f"/users/{uid}/stats?period=months&buckets=3").json()
    assert len(body["buckets"]) == 3
    assert [b["label"] for b in body["buckets"]][-1] == TODAY.strftime("%b")
    assert body["buckets"][-1]["spent"] == 12.00
    # Only one month has any spending, so there is no trend to report yet.
    assert body["summary"] == "Not enough history yet — 0% of spending wasted so far."


def test_empty_stats_do_not_divide_by_zero(client, make_user):
    uid = make_user("empty@example.com")
    body = client.get(f"/users/{uid}/stats").json()
    assert body["spent"] == 0 and body["wasted"] == 0
    assert body["waste_percent_first"] == 0.0
    assert body["summary"] == "Nothing bought in this period yet."


def test_trend_ignores_buckets_with_no_spending(client, make_user):
    """A bucket from before the user joined is 0% waste only because it is empty."""
    uid = make_user("newjoiner@example.com")

    early = add(client, uid, "Spinach", days=-10, price=10.00, bought=14)
    client.post(f"/grocery-items/{early['id']}/resolve",
                json={"outcome": "wasted", "resolved_on": iso(-14)})
    late = add(client, uid, "Rice", days=300, price=10.00, bought=0)
    client.post(f"/grocery-items/{late['id']}/resolve", json={"outcome": "used"})

    body = client.get(f"/users/{uid}/stats?period=weeks&buckets=6").json()
    assert body["buckets"][0]["spent"] == 0          # nothing that far back
    assert body["waste_percent_first"] == 100.0      # the first week they bought anything
    assert body["waste_percent_last"] == 0.0
    assert body["summary"] == "Waste is down from 100% of spending to 0% over 6 weeks."


def test_single_period_of_history_says_so(client, make_user):
    uid = make_user("firstweek@example.com")
    item = add(client, uid, "Spinach", days=-1, price=10.00, bought=0)
    client.post(f"/grocery-items/{item['id']}/resolve", json={"outcome": "wasted"})

    body = client.get(f"/users/{uid}/stats").json()
    assert body["summary"] == "Not enough history yet — 100% of spending wasted so far."
