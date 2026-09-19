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
| `FavoriteFood` | a food the user picked at onboarding + its LLM-generated `ingredients` (JSONB), with `cuisine` and an `ingredient_status` |

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

The onboarding screen sends the foods the user picked:

```bash
curl -X POST localhost:8000/users/1/favorite-foods \
     -H 'content-type: application/json' \
     -d '{"foods": ["Pad Thai", "Jollof Rice"]}'
```

It returns **202** immediately with each food `pending`. The ingredient lookup
runs in the background and the client polls `GET /users/1/favorite-foods` until
`ingredient_status` becomes `ready`:

```json
{ "name": "Pad Thai", "cuisine": "Thai", "ingredient_status": "ready",
  "ingredients": [{"name": "rice noodles", "category": "grain", "essential": true}] }
```

Endpoints: `POST|GET /users/{id}/favorite-foods`,
`POST /users/{id}/favorite-foods/{food_id}/refresh-ingredients` (retry a
`failed` row), `DELETE /users/{id}/favorite-foods/{food_id}`.

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
select name from favorite_food where ingredients @> '[{"name":"rice noodles"}]';
```

Check the endpoint from your own network:

```bash
python -m scripts.check_llm                      # the configured LLM_MODEL
python -m scripts.check_llm openai/gpt-oss-20b   # a known-fast model
```

## Demo data

```bash
python -m scripts.seed            # skips if already seeded
python -m scripts.seed --reset    # wipe demo data and redo
```

5 users, 31 grocery items and 6 friendships in assorted states. Expiry dates
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
and at anything needing a data backfill.

Two gotchas already handled: SQLite can't `ALTER` a column, so `env.py` turns on
`render_as_batch` for SQLite only; and Postgres keeps an enum type after its
table is dropped, so the initial migration's `downgrade()` drops `friendstatus`
explicitly.

## Notes

`LLM_MODEL` is whatever your endpoint serves. The code is provider-agnostic —
any OpenAI-compatible `LLM_BASE_URL` works.

There is no auth — `user_id` in the path is the acting user. Add a real auth
dependency before exposing this beyond local use.
