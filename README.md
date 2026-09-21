# Grocery Tracker API

FastAPI + SQLModel service for tracking groceries, sharing with friends, and
catching items before they expire.

## Run

```bash
pip install -r requirements-dev.txt
cp .env.example .env      # then paste your DATABASE_URL in
alembic upgrade head      # create/update the schema
python -m scripts.seed    # optional: demo data
uvicorn app.main:app --reload
```

Docs at http://localhost:8000/docs.

`DATABASE_URL` is read from `.env` (gitignored). It accepts the connection
string Supabase gives you verbatim — a bare `postgresql://` URL is rewritten to
use psycopg 3, and SSL is required by default (`DATABASE_SSLMODE` to change).
With no `DATABASE_URL` set it falls back to local `sqlite:///./grocery.db`, so
the app still runs offline.

```bash
pytest     # always runs on in-memory sqlite, never touches your real DB
```

## Data model

| Entity | Notes |
| --- | --- |
| `User` | `id`, unique `username` (the "User ID"), `password_hash`, `buddy`, optional unique `email`, `name`, plus the taste profile (`diets`, `avoid_allergens`, `favorite_cuisines`) — table is `app_user`, since `user` is reserved in Postgres |
| `GroceryItem` | belongs to one user; `expires_on`, `quantity`/`unit`, `category`, `price`, the shelf fields `shelf_life_days`, `spoilage_profile`, `shelf_buddy`, and how it ended: `consumed`, `outcome`, `resolved_on`, `rescued` |
| `Friend` | association entity between two users: `user_id`, `friend_id`, `status` (`pending`/`accepted`/`blocked`) |
| `Feast` | a planned meal: `name`, host, optional `scheduled_for`, a **snapshot** of the chosen recipe (JSONB), and its `status` (`planned`/`rescued`/`failed`) with `outcome_at` |
| `FeastAttendee` | association entity: one person invited to one feast, with their `response` (`invited`/`accepted`/`declined`) |
| `Notification` | a user's in-app message, with `delivery_status` for the outbound channel |
| `FoodPreference` | a food the user likes **or dislikes** + its LLM-generated `ingredients` (JSONB), with `preference`, `cuisine` and an `ingredient_status` |

A friendship pair is stored **once**, in whichever direction it was requested;
`status` is what makes it mutual. Deleting a user cascades to their items and
friendships.

## Endpoints

**Expiry (the main one)**

- `GET /users/{user_id}/grocery-items/expiring` — one user's soon-to-expire items
- `GET /grocery-items/expiring` — same across all users, or scoped with `?user_id=`

Query params (both): `within_days` (default `3`), `include_friends` (pull in
accepted friends' items), `include_expired` (default `true`), `include_consumed`
(default `false`). Results are sorted soonest-first and each carries
`days_until_expiry`, `expired`, and `owner_name`. Items with no `expires_on`
are never returned.

```bash
curl "localhost:8000/users/1/grocery-items/expiring?within_days=5&include_friends=true"
```

**Accounts** — `POST /auth/signup` (same handler as `POST /users`),
`POST /auth/login`, `PUT /users/{id}/password`

**Users** — `GET /users` (`?username=` for an exact User ID lookup),
`GET|PATCH|DELETE /users/{id}`, `GET|PUT /users/{id}/taste-profile`

**Shelf** — `GET /users/{id}/shelf`,
`GET /grocery-items/freshness-preview?name=`,
`POST /grocery-items/{id}/resolve` (used/wasted)

**Waste and spending** — `GET /users/{id}/stats?period=weeks|months&buckets=6`

**Grocery items** — `POST|GET /users/{user_id}/grocery-items` (list filters:
`category`, `consumed`, `expires_before`), `GET|PATCH|DELETE /grocery-items/{id}`

**Friends** — `POST /users/{user_id}/friends` (request by id),
`POST /users/{user_id}/friends/invite` (request by User ID),
`GET /users/{user_id}/friends?status=accepted`,
`GET /users/{user_id}/friends/{friend_id}/shelf` (their yellow/red buddies, no prices),
`PATCH /users/{user_id}/friends/{friend_id}` (accept/block),
`DELETE /users/{user_id}/friends/{friend_id}`

## Accounts

The join screen collects a User ID, a password and a buddy — no email:

```bash
curl -X POST localhost:8000/auth/signup -H 'content-type: application/json' \
     -d '{"username": "pantry.pal", "password": "cold-storage-9", "buddy": "carl"}'
```

`username` is the login handle and how friends find each other. It is stored
lowercased, must be 3-40 characters of letters, digits, dot, dash or
underscore, and is unique. `email` is still accepted and still unique, but it
is optional: nothing on the screen asks for one, and the in-app inbox does not
need it. `name` defaults to the User ID until the profile screen sets one.
`buddy` is one of `sammy`, `milo`, `eddie`, `carl`, `bella`.

Passwords are stored as PBKDF2-HMAC-SHA256 (240k iterations, per-password
salt, stdlib only — no native build step) and never come back out of the API.
`POST /auth/login` checks them and returns the user. A wrong password and an
unknown User ID give the same 401, so accounts cannot be enumerated.

**There are still no session tokens.** Login verifies credentials and hands
back the user; every other endpoint takes `user_id` in the path, unauthenticated.
Issuing a token is a change to `app/routers/auth.py` plus a dependency on the
other routers — the stored credentials are already in the right shape for it.

## Taste profile

The second onboarding screen — diets, allergens and loved cuisines:

```bash
curl -X PUT localhost:8000/users/1/taste-profile -H 'content-type: application/json' \
     -d '{"diets": ["Pescatarian"], "avoid_allergens": ["Peanuts"],
          "favorite_cuisines": ["Italian", "Mexican"]}'
```

The labels the UI shows are accepted as-is (`"Gluten-free"` → `gluten_free`,
`"Tree nuts"` → `tree_nuts`). Diets are `vegetarian`, `vegan`, `pescatarian`,
`gluten_free`, `dairy_free`; allergens are `peanuts`, `shellfish`, `tree_nuts`,
`sesame`. Cuisines are free text, title-cased and de-duplicated.

Each list **replaces** the previous one rather than merging — de-selecting the
last chip has to stick. Omit a field to leave it alone.

All three live on `app_user` as JSON columns: short fixed lists, always read
with the user, never queried on their own. Three join tables would buy nothing.

**Allergens are enforced, not merely suggested** — see
[Recipe suggestions](#recipe-suggestions).

## The shelf

`GET /users/{id}/shelf` is the home screen in one call: every unconsumed item
with its freshness chip, plus the counts printed above them.

```json
{ "user_id": 1, "fresh": 3, "use_soon": 1, "use_now": 2, "expired": 1,
  "needs_rescue": 3, "rescue_value": 6.99,
  "items": [ { "name": "Spinach", "price": 3.49, "expires_on": "2026-09-26",
               "shelf_life_days": 7, "spoilage_profile": "gradual",
               "shelf_buddy": "leaf", "days_until_expiry": 2,
               "freshness": "use_now", "freshness_label": "2 days" } ] }
```

`days_until_expiry`, `freshness` and `freshness_label` are **derived on read**,
never stored — they are a function of today's date, so a stored copy is wrong
by morning. The bands: `use_now` ≤ 2 days, `use_soon` ≤ 5 days, `fresh` beyond
that, `expired` past the date, `unknown` when no date is recorded.
`needs_rescue` is `use_now + expired`, and `rescue_value` totals their prices.

### Adding an ingredient

Only `name` is required. Everything else the add sheet shows is filled in:

```bash
curl -X POST localhost:8000/users/1/grocery-items -H 'content-type: application/json' \
     -d '{"name": "Spinach", "price": 3.49}'
```

- `purchased_on` defaults to today — the freshness timer starts when it was
  bought, not when it was typed in. Back-date it and the expiry moves with it.
- `shelf_life_days`, `spoilage_profile` (`gradual`/`sudden`/`stable`),
  `shelf_buddy` and `category` come from the catalogue in `app/freshness.py`.
- `expires_on` is `purchased_on + shelf_life_days`. An explicit `expires_on`
  always wins — a date read off the packet beats anything inferred from a name.
  Send `"expires_on": null` for something with no timer at all (salt, sugar).

`GET /grocery-items/freshness-preview?name=Spinach` returns exactly what saving
would store, so the sheet can show it while the user is still typing:

```json
{ "name": "Spinach", "shelf_life_days": 7, "spoilage_profile": "gradual",
  "shelf_buddy": "leaf", "category": "produce", "expires_on": "2026-09-27",
  "summary": "Gradual · ~7 days" }
```

**Why a static catalogue and not the LLM.** The preview has to answer on every
keystroke; a round-trip there would be slow, billed, and would not give the
same answer twice. The answer for "spinach" does not change. Anything not in
the catalogue falls back to 7 days / `gradual` / `leaf` — deliberately short,
since over-estimating shelf life is how food quietly rots.

The values are copied onto the row at write time rather than re-derived on
read: editing the catalogue later must not silently move an existing item's
expiry date. `shelf_buddy` is one of the keys in `freshness.BUDDY_KEYS`, which
a test pins so the catalogue cannot invent a sprite the front end has no art for.

## Onboarding & ingredients

The onboarding screen sends both lists in one call:

```bash
curl -X POST localhost:8000/users/1/food-preferences \
     -H 'content-type: application/json' \
     -d '{"likes": ["Pad Thai", "Jollof Rice"], "dislikes": ["Durian"]}'
```

Either list may be empty, but not both. Likes and dislikes share one table:
they hold identical data and are nearly always read together ("suggest
something they like, avoiding anything containing an ingredient they don't").
The unique constraint is on `(user_id, name)` and ignores `preference`, so a
user cannot both like and dislike the same food — if one confirmation contains
both, the first mention wins and the other comes back in `skipped`.

**Ingredients are fetched for dislikes too.** That is the point of storing
them: a dish gets ruled out by what is *in* it, not just by its name.

```sql
-- everything this user wants to avoid, derived from their dislikes
select distinct i->>'name'
from food_preference f
cross join lateral jsonb_array_elements(f.ingredients) i
where f.user_id = 1 and f.preference = 'dislike'
  and (i->>'essential')::boolean;
```

It returns **202** immediately with each food `pending`. The ingredient lookup
runs in the background and the client polls `GET /users/1/food-preferences`
until `ingredient_status` becomes `ready`:

```json
{ "name": "Pad Thai", "preference": "like", "cuisine": "Thai", "ingredient_status": "ready",
  "ingredients": [{"name": "rice noodles", "category": "grain", "essential": true}] }
```

Endpoints: `POST|GET /users/{id}/food-preferences` (filter with
`?preference=like` or `?preference=dislike`),
`POST /users/{id}/food-preferences/{id}/refresh-ingredients` (retry a `failed`
row), `DELETE /users/{id}/food-preferences/{id}`.

**Why the lookup is not inline.** These models can take a minute or more, and
an onboarding request that blocks that long times out at proxies and load
balancers. So the picks are committed first and enriched after: a failed
lookup leaves the row `failed` with the reason in `ingredient_error`, and the
user never has to choose their food twice. `ingredient_status` is the contract:

| status | meaning |
| --- | --- |
| `pending` | saved; lookup queued or running |
| `ready` | `ingredients` populated |
| `failed` | lookup failed or the model did not recognise the dish — call the refresh endpoint |

The model's reply is parsed and validated against a Pydantic schema
(`app/llm.py`) before it is stored, so malformed or hallucinated shapes become
a `failed` row rather than junk in the database. `ingredients` is a JSONB
column on Postgres, so it is directly queryable:

```sql
select name from food_preference where ingredients @> '[{"name":"rice noodles"}]';
```

Check the endpoint from your own network:

```bash
python -m scripts.check_llm                   # the configured LLM_MODEL
python -m scripts.check_llm some/other-model  # try a different one
```

## The You screen

### Account

`PATCH /users/{id}` changes the User ID (and name, buddy, email);
`PUT /users/{id}/password` changes the password. Both report a taken User ID as
a 409 naming *which* field clashed, since email and User ID are both unique and
only one of them is on the screen.

Changing a User ID is safe: it is a display handle, not the row key, so
friendships, items and feasts all point at `id` and nothing moves with it.

### Friends

The invite box collects a handle, not an internal id:

```bash
curl -X POST localhost:8000/users/1/friends/invite \
     -H 'content-type: application/json' -d '{"username": "maya.cooks"}'
```

`GET /users/{id}/friends` carries `needs_rescue` per row — the "2 buddies need
rescuing" line — counted for every friend in **one** query rather than one per
row.

`GET /users/{id}/friends/{friend_id}/shelf` is what a friend may see:

- **Only the yellow and red buddies.** A friend is being shown what needs
  cooking, not an inventory of someone's fridge.
- **Never a price.** The screen promises "prices stay private", so the response
  model (`FriendShelfItem`) has no price field at all — the promise cannot be
  broken by forgetting to strip one.
- **Accepted friendships only.** Pending or blocked is a 403, not a peek.

### Waste and spending

```bash
curl "localhost:8000/users/1/stats?period=weeks&buckets=6"
```

```json
{ "spent": 312.00, "wasted": 26.90, "rescued": 39.85,
  "buckets": [ {"label": "W1", "starts_on": "2026-08-10", "ends_on": "2026-08-16",
                "spent": 48.00, "wasted": 8.16, "spent_and_used": 39.84,
                "rescued": 3.25, "items_wasted": 1, "items_rescued": 1} ],
  "waste_percent_first": 17.0, "waste_percent_last": 2.1,
  "summary": "Waste is down from 17% of spending to 2.1% over 6 weeks.",
  "priced_items": 64, "unpriced_items": 0 }
```

`spent_and_used` and `wasted` are the two parts of each stacked bar, and sum to
`spent`. Weeks run Monday-Sunday with the current week last; months are
calendar months.

**Spending is attributed to when something was bought, waste and rescues to
when they were resolved.** An item bought in W1 and binned in W3 is W1's
spending and W3's waste — which is how anyone reading the chart would expect it
to behave. (`spent_and_used` is clamped at zero, since a bar can contain waste
from something bought before the window.)

**Items with no price are reported, not treated as free.** They come back in
`unpriced_items` rather than quietly dragging every total down.

**The trend compares the earliest bucket that has spending in it**, not
literally the first bar. A month from before the user joined is 0% waste only
because it is empty, and "waste is up from 0%" is a misreading of an empty bar.
With fewer than two such buckets there is no trend to report and `summary` says
so instead of inventing one.


### Solo vs friends rescues

`rescued` splits by where the food was eaten. Pass `feast_id` when resolving:

```bash
curl -X POST localhost:8000/grocery-items/12/resolve \
     -H 'content-type: application/json' \
     -d '{"outcome": "used", "feast_id": 4}'
```

Every bucket and the top-level totals carry the split:

| field | meaning |
| --- | --- |
| `rescued_solo` / `rescued_friends` | money, and they sum to `rescued` |
| `items_rescued_solo` / `items_rescued_friends` | counts, and they sum to `items_rescued` |

A missing `feast_id` means solo — which is also what every rescue recorded
before feasts were tracked was, so old rows need no backfill and read
correctly.

`feast_id` is checked: the feast must exist (404) and the item's owner must
actually be going to it (422). Without that, any id at all would land in
`rescued_friends` and the split would be whatever a client felt like claiming.

Attribution is stored for any outcome, not just rescues — food binned after a
feast is still food that feast is answerable for — but only rescues are ever
split by it.

### Why `outcome` exists

`consumed` was a single flag: gone. The whole point of this screen is the
difference between *eaten* and *binned*, which that flag cannot express. So an
item now ends its life through:

```bash
curl -X POST localhost:8000/grocery-items/12/resolve \
     -H 'content-type: application/json' -d '{"outcome": "wasted"}'
```

| field | meaning |
| --- | --- |
| `outcome` | `on_shelf`, `used` (eaten) or `wasted` (binned) |
| `resolved_on` | when it left the shelf — defaults to today |
| `rescued` | it was *used* while already in the use-now or expired band |
| `rescued_feast_id` | the feast that ate it; **null means solo** |

`rescued` is decided at the moment of the change, not derived later: once an
item is off the shelf, its expiry date no longer says how close a call it was.

This is also why the shelf resolves items instead of deleting them — **a
deleted row cannot be counted as waste**, and the chart would flatter the user
every time they tidied up.

`consumed` is kept and moved in step, so existing callers keep working:
`PATCH {"consumed": true}` records `used`, and setting it back to false clears
the outcome and puts the item back on the shelf.

## Recipe suggestions

What could these people cook together, right now?

```bash
curl -X POST localhost:8000/recipes/suggest \
     -H 'content-type: application/json' \
     -d '{"user_ids": [1, 2], "max_results": 4}'
```

It pools everyone's pantry, pools their food preferences, and asks the LLM for
recipes. Each suggestion comes back as:

```json
{ "rank": 1,
  "rank_reason": "Uses expiring: Greek yogurt, Chicken thighs, Rocket",
  "name": "Greek Yogurt Chicken Wrap",
  "cuisine": "Mediterranean",
  "prep_minutes": 10, "cook_minutes": 10, "total_minutes": 20,
  "uses": [ {"name": "Greek yogurt",  "from_users": ["Sam Perera"],  "expiring": true},
            {"name": "Spring onions", "from_users": ["Mei Tanaka"],  "expiring": true} ],
  "missing": ["pancetta"],
  "contributors": ["Sam Perera", "Mei Tanaka"],
  "liked_by": [], "is_liked": false }
```

**Who brings what.** Every entry in `uses` names the users who actually have
that item, and `contributors` is the set of users supplying at least one
ingredient. Both are read from `grocery_item` — the model is never asked who
owns anything. One ingredient held by two people collapses to a single entry
listing both (matched on a normalised name, so "Rice" and " rice " are one
thing).

**How long it takes.** `prep_minutes` and `cook_minutes` come from the model;
`total_minutes` is their sum, and is `null` unless both are known. A time that
is missing, non-numeric, or absurd (over 8 hours) becomes `null` rather than
being shown or used for ranking.

**Ranking.** `rank` is 1-based and reflects the server's own ordering;
`rank_reason` names the rule that earned the position. The keys, in order:

1. Dishes someone likes (`"Liked by Ann"`)
2. Then dishes using the most about-to-expire ingredients (`"Uses expiring: spinach"`)
3. Then the quickest by `total_minutes` (`"Cookable in 12 min"`)

Speed is only a tiebreak — a liked dish outranks a faster one.

The rules, and where each is enforced:

| Rule | Enforced |
| --- | --- |
| Never suggest anything containing a group allergen | **In code**, after the model replies |
| Never suggest a dish breaking a group diet | **In code**, after the model replies |
| Never suggest a dish anyone in the group dislikes | **In code**, after the model replies |
| Rank liked dishes first | In code (`is_liked` is matched against the DB, not claimed by the model) |
| Fall back to other dishes when no liked one is cookable | In code — `detail` says so |
| Prefer ingredients that are about to expire | Prompt, then re-ranked in code |
| Who owns each ingredient | In code, from `grocery_item` |

**The model is instructed, but not trusted.** It is told the rules in the
prompt, and every rule that matters is applied again server-side — a prompt is
guidance, not a guarantee. Four things are recomputed rather than believed: `is_liked` (matched against
`food_preference`), `uses` (resolved against the real pantry, so anything
invented is dropped and real owners are attached), `uses_expiring` — in testing
the model cheerfully labelled 300-day-old lentils as expiring, and that field
drives the ranking — and the ordering itself.

Expired and consumed groceries are excluded from the pantry: a recipe built on
food that has already gone off is worse than no suggestion.

### Allergies and diets

The onboarding screen promises allergens "never show up in your recipes, or in
Feast recipes with friends", so they are checked twice: the prompt is told, and
every suggestion is then matched against the keyword lists in `app/dietary.py`
before it reaches the client. A hit drops the suggestion outright — the
response reports `excluded_for_dietary_rules` and, if nothing survives, says so
in `detail`. The check covers the dish name, what it uses *and* what is still
to buy: tahini in the shopping list is still sesame.

Constraints are pooled across everyone eating and every one of them applies:
one person's peanut allergy rules peanuts out of the shared meal, and the
strictest diet in the group is the one the meal has to satisfy.

`POST /feasts` re-runs the same check against the actual guest list, because
the suggestion was filtered for whoever it was generated for — which may be a
different set, or the same one after somebody added an allergy. A clash is a
422 naming the offending ingredient rather than a feast nobody can eat.

Favourite cuisines are the soft counterpart: they go into the prompt as a
preference and never exclude anything.

**A dislike outranks a like.** If one person likes a dish and another dislikes
it, it is excluded — the meal is shared.

Note that dislikes are matched by *dish*, not by ingredient. Ingredient-level
avoidance sounds better but is a trap: Sam dislikes "Liver and Onions", whose
essential ingredients include onions, so avoiding every disliked ingredient
would rule out most of the cookbook.

## Feasts

The front end posts back whichever suggestion the user picked:

```bash
curl -X POST localhost:8000/feasts -H 'content-type: application/json' -d '{
  "name": "Saturday Cook-Up",
  "host_id": 1,
  "attendee_ids": [3, 5],
  "scheduled_for": "2026-09-26T19:00:00Z",
  "recipe": { ...one object straight from /recipes/suggest... }
}'
```

**The recipe is snapshotted, not referenced.** Suggestions are generated on
demand and stored nowhere, so a foreign key would dangle the moment the
suggestion call returned. Storing the whole object also keeps the feast honest:
it records what people agreed to, even after pantries and preferences move on.

The host is added as an attendee automatically and starts `accepted` — they do
not get an invitation to their own feast. Duplicate attendee ids collapse.

Endpoints: `POST /feasts`, `GET /feasts/{id}`, `GET /users/{id}/feasts`
(`?hosting_only=true`), `POST /feasts/{id}/respond/{user_id}` (accept/decline),
`POST /feasts/{id}/resend-invitations`.

### Feast outcomes

A feast starts `planned`. The host records how it went:

```bash
curl -X POST localhost:8000/feasts/4/outcome -H 'content-type: application/json' \
     -d '{"host_id": 1, "status": "rescued"}'
```

**Host only** — anyone else gets a 403 — and a feast cannot be set back to
`planned`, since "it has not happened yet" is not something you learn later.

`rescued` also books the groceries it used: each is marked eaten and attributed
to the feast, which is what moves it into `rescued_friends`. Which groceries?

- `item_ids` if you send them.
- Otherwise the recipe snapshot is matched by name against the attendees'
  shelves. The snapshot already records what the meal was made of, so it is the
  natural source; it is also why a feast created before its ingredients were
  added matches nothing (`uses` only ever contains things somebody had).

Items belonging to people who are not attending are skipped rather than
refused — a guest list can change after the recipe was chosen. Items already
off the shelf are skipped too, so recording the outcome twice does not move an
earlier rescue onto this feast and count it again.

**Being at a feast does not by itself make something a rescue.** The rule is
unchanged: the item had to be in the use-now or expired band when it was eaten.
A feast that used up a bag of rice bought yesterday was a feast, not a rescue.
The response reports `items_used` and `items_rescued` separately so the app can
say which happened.

**The response carries no prices.** The host acts on other attendees' groceries
here, and `FeastOutcomeResult` has no field for what any of it cost.

### Invitations

Creating a feast writes a `notification` row per guest, then delivers them in
the background. The row is the durable record and the user's in-app inbox:
`GET /users/{id}/notifications` (`?unread_only=true`) and
`POST /notifications/{id}/read`.

The invitation is assembled from the snapshot, so it tells each guest what to
bring:

> **Sam Perera invited you to Saturday Cook-Up**
> Sam Perera is cooking Salmon and Rocket Salad. When: Sat 26 Sep, 19:00.
> Takes about 15 minutes. Bringing: Theo Alvarez — Salmon fillet, Rocket;
> Mei Tanaka — Spring onions; Sam Perera — Greek yogurt, Olive oil.
> Still to buy: lemon juice.

**Delivery is decoupled from creation.** A slow or broken channel must not stop
a feast being created, so sending happens after the response. That is why
`POST /feasts` returns `invitations_pending: 2, invitations_sent: 0` — read
`GET /feasts/{id}` for what actually went out. A channel that throws marks its
row `failed` with the reason in `delivery_error`; `resend-invitations` retries
only rows that are not yet `sent`, so it can be run repeatedly without
double-inviting anyone.

**Channels.** No email or push provider is configured here, so the default
channel writes the invitation to the application log and marks the row sent.
Set `NOTIFICATION_CHANNEL=null` to disable, or add a real channel to `CHANNELS`
in `app/notifications.py` — the inbox, retry and failure handling are already
in place around it.

## Demo data

```bash
python -m scripts.seed                # skips if already seeded
python -m scripts.seed --reset        # wipe demo data and redo
python -m scripts.seed --with-foods   # also seed food preferences (makes real LLM calls)
python -m scripts.seed --with-feasts  # also plan feasts and send invitations
```

5 users, 31 grocery items and 6 friendships in assorted states. `--with-foods`
adds 11 food preferences (7 likes, 4 dislikes); `--with-feasts` adds 2 feasts
with 6 attendees and 4 delivered invitations, one guest accepting and one
declining on each. Both are opt-in because filling them in means real (billed)
LLM calls.

`--with-feasts` picks each feast's recipe by running the real suggestion logic
against the seeded pantries, so the stored snapshot is a genuine suggestion
rather than a hand-written fake — and it works on an already-seeded database,
since feasts depend only on the users and their groceries. Expiry dates
are **relative to the day you run it**, so a handful of items are always
expired, expiring today, and expiring this week — the `/expiring` endpoints
stay interesting without re-seeding.

Every demo account shares the password `fridge-friends-demo` (User IDs
`sam.perera`, `nadia.k`, `theo.a`, `mei.t`, `obi.n`), so the join screen can be
exercised against seeded data. Two accounts carry a taste profile, which is
what makes the allergen filtering visible: Nadia is vegetarian and allergic to
peanuts, Mei avoids shellfish, Obi is dairy-free.

Every demo account is on `@grocerydemo.dev`, and `--reset` deletes only those
accounts. It never truncates tables, so it is safe to run against a database
that also holds real rows.

## Migrations

Alembic owns the schema; the app does **not** create tables at startup, so a new
checkout (or a deploy) needs `alembic upgrade head`.

```bash
alembic upgrade head                              # apply everything
alembic revision --autogenerate -m "add x"        # after editing app/models.py
alembic check                                     # models vs. DB drift, no changes made
alembic downgrade -1                              # step back one
alembic history                                   # what exists
```

The connection string comes from `app.database` (i.e. `.env`) — `alembic.ini`
has no URL in it, so there is one place to change and no credential in a
tracked file.

Always read an autogenerated migration before applying it. Autogenerate is good
at tables, columns and indexes, and weak at renames (it sees a drop plus an add)
and at anything needing a data backfill. The `food_preference` migration is
hand-written for exactly that reason — autogenerating the `favorite_food`
rename would have dropped every row.

The history here is not linear by accident: `d82a3e9a4925` was written in a
second working copy of this repo and applied to Supabase without being
committed, so `c4a17e9b52d1` was rebased onto it rather than branching from the
same parent. Keep new revisions on one chain — alembic cannot upgrade a
database sitting on a revision it has never heard of.

Three gotchas already handled. SQLite can't `ALTER` a column, so `env.py` turns
on `render_as_batch` for SQLite only. Postgres keeps an enum type after its
table is dropped, so the initial migration's `downgrade()` drops `friendstatus`
explicitly. And batch mode on SQLite rebuilds a table by dropping it — which
fires `grocery_item`'s `ON DELETE CASCADE` and silently empties the table — so
`c4a17e9b52d1` turns foreign keys off around its batch operations, inside an
`autocommit_block()` because SQLite ignores that PRAGMA within a transaction.

## Profile photos

`avatar_url` is a nullable string on `UserRead` and `UserUpdate`:

```bash
curl -X PATCH localhost:8000/users/1 -H 'content-type: application/json' \
     -d '{"avatar_url": "https://example.com/me.jpg"}'
```

Send `null` or `""` to clear it and fall back to the `buddy` sprite. Only
`http://` and `https://` links are accepted — the app renders this straight
into an image tag, so `javascript:` and `data:` URLs are refused here rather
than trusted downstream.

**This stores a link, not a file.** There is no upload endpoint, because there
is no object storage wired up to put the bytes in: that needs an S3 or Supabase
Storage bucket, credentials, and a signed-upload flow. Point this at whatever
you already use for image hosting, or say the word and it can be added once a
bucket exists.

## CORS

A browser front end on another origin needs this, and the symptom when it is
missing is misleading: the preflight `OPTIONS` comes back `405`, the browser
never sends the real request, and the server logs stay empty — so it reads as
"the backend is unreachable" when the backend is fine.

`CORS_ORIGINS` is a comma-separated list of allowed origins and defaults to
`*`. Credentials are only enabled when an explicit list is given: the spec
forbids pairing `allow_credentials` with `*`, and browsers reject that
combination outright rather than falling back to something weaker.

```bash
CORS_ORIGINS=http://localhost:3000,https://fridge-friends.example
```

## Notes

`LLM_MODEL` is whatever your endpoint serves — currently `openai/gpt-oss-20b`
on NVIDIA NIM, which answers in a few seconds. The code is provider-agnostic:
any OpenAI-compatible `LLM_BASE_URL` works, and switching models is a one-line
change in `.env`. (`z-ai/glm-5.3` is listed by that endpoint but never
responded during development — requests disconnect at 60s.)

Passwords are stored and checked, but there are no session tokens yet:
`user_id` in the path is still the acting user, and no endpoint verifies who is
calling. Add a token and an auth dependency before exposing this beyond local
use.
