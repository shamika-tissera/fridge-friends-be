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
| `User` | `id`, unique `email`, `name`, `created_at` — table is `app_user`, since `user` is reserved in Postgres |
| `GroceryItem` | belongs to one user; `expires_on`, `consumed`, `quantity`/`unit`, `category` |
| `Friend` | association entity between two users: `user_id`, `friend_id`, `status` (`pending`/`accepted`/`blocked`) |
| `Feast` | a planned meal: `name`, host, optional `scheduled_for`, and a **snapshot** of the chosen recipe (JSONB) |
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

**Users** — `POST /users`, `GET /users`, `GET|PATCH|DELETE /users/{id}`

**Grocery items** — `POST|GET /users/{user_id}/grocery-items` (list filters:
`category`, `consumed`, `expires_before`), `GET|PATCH|DELETE /grocery-items/{id}`

**Friends** — `POST /users/{user_id}/friends` (request),
`GET /users/{user_id}/friends?status=accepted`,
`PATCH /users/{user_id}/friends/{friend_id}` (accept/block),
`DELETE /users/{user_id}/friends/{friend_id}`

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

Two gotchas already handled: SQLite can't `ALTER` a column, so `env.py` turns on
`render_as_batch` for SQLite only; and Postgres keeps an enum type after its
table is dropped, so the initial migration's `downgrade()` drops `friendstatus`
explicitly.

## Notes

`LLM_MODEL` is whatever your endpoint serves — currently `openai/gpt-oss-20b`
on NVIDIA NIM, which answers in a few seconds. The code is provider-agnostic:
any OpenAI-compatible `LLM_BASE_URL` works, and switching models is a one-line
change in `.env`. (`z-ai/glm-5.3` is listed by that endpoint but never
responded during development — requests disconnect at 60s.)

There is no auth — `user_id` in the path is the acting user. Add a real auth
dependency before exposing this beyond local use.
