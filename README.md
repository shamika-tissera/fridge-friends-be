# Fridge Friends API

The backend for Fridge Friends, an app for keeping track of what is in your kitchen, using it before it goes off, and cooking with friends when there is too much to eat alone.

You add groceries and the server works out how long each one will last. Anything close to its date shows up on your shelf as something to use up. Friends can see each other's soon-to-expire food and plan a shared meal around it, called a feast. The app also tracks what you spent, what you wasted, and what you saved by cooking things in time.

## Stack

- FastAPI and Pydantic v2
- SQLModel (SQLAlchemy underneath) with Alembic migrations
- Postgres in production, developed against Supabase. SQLite when no `DATABASE_URL` is set
- Any OpenAI-compatible LLM endpoint for ingredient lookups and recipe suggestions
- Expo push notifications for feast invitations, plus an in-app inbox

## Getting started

You need Python 3.11 or newer.

```bash
git clone https://github.com/shamika-tissera/fridge-friends-be.git
cd fridge-friends-be
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
alembic upgrade head
python -m scripts.seed
uvicorn app.main:app --reload
```

With no configuration this runs against a local SQLite file, `grocery.db`. The API is at http://localhost:8000 and the interactive docs are at http://localhost:8000/docs. The seed step is optional and is described under [Demo data](#demo-data). `requirements.txt` holds only the runtime dependencies, in case you do not want pytest.

### Configuration

Settings are environment variables. For local work, copy `.env.example` to `.env` and edit it. A real environment variable always wins over the file.

| Variable | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./grocery.db` | Postgres or SQLite. A bare `postgresql://` or `postgres://` URL, which is what Supabase gives you, is switched to psycopg 3 automatically. |
| `DATABASE_SSLMODE` | `require` | Postgres only. |
| `LLM_API_KEY` | none | Needed for ingredient lookups and recipe suggestions. |
| `LLM_BASE_URL` | `https://integrate.api.nvidia.com/v1` | Any OpenAI-compatible endpoint. |
| `LLM_MODEL` | `openai/gpt-oss-20b` | Whatever your endpoint serves. |
| `LLM_TIMEOUT_SECONDS` | `90` | How long to wait on the LLM before giving up. |
| `NOTIFICATION_CHANNEL` | `log` | `log`, `null` or `expo`. See [Notifications](#notifications). |
| `CORS_ORIGINS` | `*` | Comma-separated list of allowed origins. |

`.env.example` ships with a placeholder `DATABASE_URL`. Fill it in with a real Postgres string, or delete the line to stay on SQLite.

Only two features call the LLM: the ingredient lookup after onboarding and `POST /recipes/suggest`. Without a key, recipe suggestions return 503 and the lookup marks its rows `failed`. Everything else works. To check that your endpoint answers, run this (it takes an optional model name):

```bash
python -m scripts.check_llm
```

### Deploying

Install `requirements.txt`, set `DATABASE_URL`, `CORS_ORIGINS` and the LLM variables in the environment, run `alembic upgrade head`, and start the server:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Read [Security and known limitations](#security-and-known-limitations) before putting it on a public address.

## Trying it out

These commands use the demo data and assume a fresh database, so Sam Perera is user 1.

```bash
# log in (returns the user record, there is no token)
curl -X POST localhost:8000/auth/login -H 'content-type: application/json' \
     -d '{"username": "sam.perera", "password": "fridge-friends-demo"}'

# the home screen: every item with its freshness chip, plus the counts above them
curl localhost:8000/users/1/shelf

# what is about to expire, including friends' items
curl "localhost:8000/users/1/grocery-items/expiring?within_days=5&include_friends=true"

# add an ingredient (only the name is required)
curl -X POST localhost:8000/users/1/grocery-items -H 'content-type: application/json' \
     -d '{"name": "Spinach", "price": 3.49}'

# bin the expired baby spinach that came with the seed data
curl -X POST localhost:8000/grocery-items/2/resolve -H 'content-type: application/json' \
     -d '{"outcome": "wasted"}'

# spending, waste and rescues over the last six weeks
curl "localhost:8000/users/1/stats?period=weeks&buckets=6"
```

## API

Every route, with its parameters and response shapes, is in the interactive docs at `/docs` (Swagger UI) or `/redoc`. The raw schema is at `/openapi.json`, and `GET /health` returns `{"status": "ok"}` for uptime checks.

Most routes are scoped to a user through the path, as in `/users/{user_id}/...`. Errors use FastAPI's `{"detail": "..."}` body. You will see 404 for a missing record, 409 for a User ID or email that is taken (the message says which), 422 for validation failures, 403 for things a user is not allowed to see or do, and 503 when the LLM cannot be reached.

### Accounts and profile

| Route | Purpose |
| --- | --- |
| `POST /auth/signup` | Create an account from a User ID, a password and a buddy. Email is optional. Same handler as `POST /users`. |
| `POST /auth/login` | Check credentials and return the user. A wrong password and an unknown User ID give the same 401. |
| `GET /users` | List users. `?username=` looks up one User ID exactly. |
| `GET`, `PATCH`, `DELETE /users/{id}` | Read, edit (User ID, name, buddy, email, avatar link) or delete. Deleting removes the user's items, friendships, food preferences, notifications and hosted feasts. |
| `PUT /users/{id}/password` | Change the password. Needs the current one. |
| `GET`, `PUT /users/{id}/taste-profile` | Diets, allergens and favourite cuisines. Each list you send replaces the old one. |
| `POST /users/{id}/push-token` | Store the device's Expo push token. |

The User ID is the login handle and how friends find each other. It is stored lowercase and must be 3 to 40 characters of letters, digits, dots, dashes or underscores, starting with a letter or digit. The buddy is one of `sammy`, `milo`, `eddie`, `carl` or `bella`. Passwords need at least 8 characters.

### Groceries and shelf

| Route | Purpose |
| --- | --- |
| `POST /users/{id}/grocery-items` | Add an item. Only `name` is required. |
| `GET /users/{id}/grocery-items` | List items. Filters: `category`, `consumed`, `expires_before`, plus `offset` and `limit`. |
| `GET`, `PATCH`, `DELETE /grocery-items/{id}` | Read, edit or delete one item. |
| `POST /grocery-items/{id}/resolve` | Mark an item `used` or `wasted`. Pass `feast_id` if it was eaten at a feast. |
| `GET /grocery-items/freshness-preview?name=` | The shelf life the server would assign to a name. No database or LLM call. |
| `GET /users/{id}/shelf` | The home screen: all items with freshness chips and rescue counts. |
| `GET /users/{id}/stats` | Spending, waste and rescues by week or month. |
| `GET /users/{id}/grocery-items/expiring` | Items close to their date. Options: `within_days` (default 3), `include_friends`, `include_expired` (default true), `include_consumed` (default false). |
| `GET /grocery-items/expiring` | The same across all users, or one user with `?user_id=`. |

### Friends

| Route | Purpose |
| --- | --- |
| `POST /users/{id}/friends` | Send a friend request by user id. |
| `POST /users/{id}/friends/invite` | Send a request by User ID, the handle a person types into the invite box. |
| `GET /users/{id}/friends` | List friendships, filtered with `?status=`. Each row has the other user and how many of their items need rescuing. |
| `GET /users/{id}/friends/{friend_id}/shelf` | A friend's yellow and red items. No prices. Accepted friends only, otherwise 403. |
| `PATCH /users/{id}/friends/{friend_id}` | Accept or block. |
| `DELETE /users/{id}/friends/{friend_id}` | Remove the friendship. |

A friendship is stored once, in whichever direction it was requested, and `status` (`pending`, `accepted` or `blocked`) is what makes it mutual.

### Food preferences

| Route | Purpose |
| --- | --- |
| `POST /users/{id}/food-preferences` | Save liked and disliked foods. Returns 202 and looks up ingredients in the background. |
| `GET /users/{id}/food-preferences` | List them. Poll until `ingredient_status` is `ready`. `?preference=like` or `dislike` filters. |
| `POST /users/{id}/food-preferences/{food_id}/refresh-ingredients` | Retry a lookup that `failed`. |
| `DELETE /users/{id}/food-preferences/{food_id}` | Remove one. |

### Recipes and feasts

| Route | Purpose |
| --- | --- |
| `POST /recipes/suggest` | Recipes cookable from the pooled pantries of one or more users. |
| `POST /feasts` | Create a feast from a suggestion and invite people. |
| `GET /feasts/{id}` | One feast, with attendees and delivery counts. |
| `GET /users/{id}/feasts` | Feasts a user hosts or is invited to. `?hosting_only=true` narrows it to hosting. |
| `POST /feasts/{id}/respond/{user_id}` | Accept or decline an invitation. |
| `POST /feasts/{id}/outcome` | The host records `rescued` or `failed`. |
| `POST /feasts/{id}/resend-invitations` | Retry any invitation that has not been delivered. |
| `GET /users/{id}/notifications` | The in-app inbox, newest first. `?unread_only=true` filters. |
| `POST /notifications/{id}/read` | Mark one notification as read. |

## How it works

### Freshness and the shelf

When you add an item, only `name` is required. The server fills in the rest from a static catalogue in `app/freshness.py`: shelf life in days, a spoilage profile (`gradual`, `sudden` or `stable`), the buddy sprite drawn on the shelf, and a category. `purchased_on` defaults to today, and `expires_on` is `purchased_on` plus the shelf life. If you send your own `expires_on`, it wins over anything guessed from the name. Send `"expires_on": null` for something that never goes off, like salt.

The catalogue is static on purpose. The add screen calls `freshness-preview` on every keystroke, and an LLM round trip there would be slow, cost money and give a different answer on a different day. Names the catalogue does not know get 7 days, `gradual`. That errs short, because a shelf life guessed too long is how food rots unnoticed. The values are copied onto the row when it is saved, so editing the catalogue later does not move the expiry date of anything already on a shelf.

Freshness is worked out each time an item is read and never stored, since it depends on today's date. The bands are:

- `use_now`: 2 days or fewer left
- `use_soon`: 5 days or fewer
- `fresh`: more than 5 days
- `expired`: past its date
- `unknown`: no date recorded

On the shelf response, `needs_rescue` is `use_now` plus `expired`, and `rescue_value` adds up their prices.

### Used, wasted and rescued

Items are resolved, not deleted. `POST /grocery-items/{id}/resolve` sets `outcome` to `used` or `wasted` and records `resolved_on`. A deleted row cannot be counted as waste, so removing things from the shelf would make the waste chart look better than the truth. The older `consumed` flag still works and is kept in step with `outcome`.

An item counts as a rescue when it is used while it is already in the `use_now` or `expired` band. That is decided at the moment it is resolved and stored as `rescued`, because once an item is gone its date no longer says how close a call it was. If the food was eaten at a feast, pass `feast_id` and the rescue counts as shared with friends. Without one it counts as solo. The feast has to exist and the item's owner has to be attending it.

`GET /users/{id}/stats` returns the totals plus one bucket per week (Monday to Sunday) or calendar month, ending with the current one. Spending lands in the bucket where the item was bought. Waste and rescues land in the bucket where they were resolved. Items with no price are counted in `unpriced_items` rather than treated as free. The trend sentence compares the earliest bucket that has any spending with the latest one, so an empty bar from before the user joined does not read as 0% waste.

### Recipe suggestions

`POST /recipes/suggest` takes one or more user ids and returns recipes the group could cook now. It pools everyone's unexpired, unconsumed groceries and everyone's food preferences, asks the LLM for recipes, and then checks the reply in code instead of trusting it.

- Allergens and diets are matched against the keyword lists in `app/dietary.py`. A suggestion that trips one is dropped, including when the ingredient is only in its "still to buy" list. The rules are pooled, so one person's peanut allergy applies to the whole table.
- Dishes that anyone dislikes are dropped. A dislike beats a like because the meal is shared. Dislikes match by dish name, not by ingredient: someone who dislikes liver and onions has not given up onions, and excluding every ingredient of every disliked dish would rule out most of the cookbook.
- `uses` is resolved against the real pantry, so invented ingredients disappear and each remaining one lists who actually has it. `uses_expiring` is recomputed as well, since the model once called a 300 day old bag of lentils "expiring" and that field drives the ranking.
- Order is decided by the server: liked dishes first, then the ones that use the most expiring ingredients, then the quickest by `total_minutes`. `rank_reason` names the rule that put each recipe where it is.

If nothing safe can be made, `detail` says so and `excluded_for_dietary_rules` counts what was dropped. The call runs inside the request, so it can be slow. It returns 503 if the LLM cannot be reached or replies with something that does not match the expected shape.

### Onboarding foods

Confirming the onboarding screen saves the picks straight away and returns 202, with each food `pending`. A background task then asks the LLM for the ingredients (one call for the whole list), and each row becomes `ready`, or `failed` with the reason in `ingredient_error`. The lookup stays out of the request because these models can take a minute or more, and a request held that long gets cut off by proxies and load balancers. A failed lookup never costs the user their picks. The reply is validated against a Pydantic schema before anything is stored.

Likes and dislikes share one table, with a unique constraint on user and name, so nobody can both like and dislike the same food. If one request lists a food in both places, the first mention wins. Foods that were already saved come back in `skipped` too.

### Feasts

A feast is a recipe, a host and a guest list. The client posts back whichever suggestion the user picked, unchanged. The recipe is stored as a JSON snapshot rather than a reference. Suggestions are generated on demand and kept nowhere else, and the feast should keep showing what people agreed to even after their pantries change.

`POST /feasts` checks the recipe against the allergies and diets of the actual guest list and answers 422, naming the ingredient, if it clashes. The list can differ from the group the suggestion was generated for, or someone may have added an allergy since. The host is added as an attendee who has already accepted. Each guest gets a `notification` row, and delivery is attempted in the background. That is why the create response shows `invitations_pending` above zero: read `GET /feasts/{id}` to see what actually went out.

Afterwards the host calls `POST /feasts/{id}/outcome` with `rescued` or `failed`. Anyone else gets a 403, and a feast cannot be set back to `planned`. On `rescued`, the groceries the meal used are marked eaten and attributed to the feast. Those are the `item_ids` you send, or by default the recipe's ingredients matched by name against the attendees' shelves. Items owned by people who are not attending are skipped, and so are items already off the shelf, so recording an outcome twice does not count anything twice.

Being at a feast does not by itself make something a rescue. The item still has to have been in the `use_now` or `expired` band when it was eaten. The response reports `items_used` and `items_rescued` separately, and it never includes prices, since the host is acting on other people's groceries.

### Notifications

Every invitation is written to the `notification` table first. That row is the guest's in-app inbox entry and the delivery record. A channel then tries to push it somewhere external. If the channel fails, the row is marked `failed` with the error in `delivery_error`, and `resend-invitations` retries only the rows that are not yet `sent`. Running it repeatedly never invites anyone twice.

`NOTIFICATION_CHANNEL` picks the channel:

- `log` writes the invitation to the application log and marks it sent. This is the default.
- `null` sends nothing.
- `expo` sends a push through Expo. Clients register their device with `POST /users/{id}/push-token`, and the channel needs the `exponent-server-sdk` package (`pip install exponent-server-sdk`). Users with no token are skipped, and their row is still marked `sent` because the inbox already has the message.

To add another channel, write a class with a `send(self, *, to, title, body)` method and register it in `CHANNELS` in `app/notifications.py`. The inbox, retries and failure handling already wrap it.

## Demo data

```bash
python -m scripts.seed                # skips if already seeded
python -m scripts.seed --reset        # remove the demo accounts and start again
python -m scripts.seed --with-foods   # also add food preferences (real LLM calls)
python -m scripts.seed --with-feasts  # also plan feasts and send invitations (implies --with-foods)
```

The base seed creates seven users with a few dozen groceries between them, and friendships in every state: accepted, pending and blocked. Every demo account uses the password `fridge-friends-demo`. Five of them have emails on `@grocerydemo.dev` (User IDs `sam.perera`, `nadia.k`, `theo.a`, `mei.t` and `obi.n`), and two were created the way the join screen creates them, with no email (`pantry.pal` and `rae.cooks`). Several carry a taste profile so you can see allergen filtering work: Nadia is vegetarian and allergic to peanuts, Mei avoids shellfish, and Obi is dairy-free.

Expiry dates are relative to the day you run the script, so there are always items that are expired, expiring today and expiring this week.

`--with-foods` and `--with-feasts` are opt-in because they make real, billed LLM calls. Feasts are planned by running the real suggestion logic against the seeded pantries, so the stored recipes are not hand-written fakes, and the flag also works on a database that was already seeded.

`--reset` only deletes the demo accounts. It never truncates tables, so it is safe on a database that also holds real rows.

## Project layout

```
app/
  main.py            app setup, CORS, router registration, /health
  database.py        engine and sessions, .env loading, URL normalising
  models.py          SQLModel tables and enums
  schemas.py         request and response models
  services.py        shelf, stats, pantry and feast logic shared by routers
  freshness.py       shelf-life catalogue and freshness bands
  dietary.py         allergen and diet rules
  llm.py             OpenAI-compatible client, prompts, reply validation
  notifications.py   delivery channels and invitation wording
  security.py        password hashing
  routers/           auth, users, groceries, friends, onboarding, recipes, feasts
migrations/          Alembic environment and versions
scripts/             seed.py and check_llm.py
tests/               pytest suite
```

There are seven tables: `app_user` (named that because `user` is reserved in Postgres), `grocery_item`, `friend`, `food_preference`, `feast`, `feast_attendee` and `notification`. Lists that are short, fixed and always read with their owner, such as a user's diets and allergens, are JSON columns instead of join tables. On Postgres the JSON columns are JSONB, so the stored ingredients can be queried directly.

## Database and migrations

Alembic owns the schema. The app does not create tables when it starts, so a fresh checkout or a new deploy needs `alembic upgrade head`.

```bash
alembic upgrade head                        # apply everything
alembic revision --autogenerate -m "add x"  # after editing app/models.py
alembic check                               # compare models with the database
alembic downgrade -1                        # step back one revision
alembic history                             # list what exists
```

The connection string comes from `app.database`, which reads `.env`. `alembic.ini` has no URL, so there is one place to change it and no credential in a tracked file.

A few things to know before you write a migration:

- Read every autogenerated migration before you apply it. Autogenerate handles tables, columns and indexes well. It sees a rename as a drop plus an add and cannot backfill data, which is why the `food_preference` migration is written by hand.
- SQLite cannot alter a column, so `migrations/env.py` turns on batch mode for SQLite only. Batch mode rebuilds a table by dropping it, which fires `ON DELETE CASCADE` and can quietly empty child tables. Revision `c4a17e9b52d1` switches foreign keys off around its batch operations for that reason.
- Postgres keeps an enum type after its table is dropped, so the initial migration's `downgrade()` drops `friendstatus` explicitly.
- Keep new revisions on one chain. Alembic cannot upgrade a database that sits on a revision it has never seen. `alembic heads` should list exactly one.

## Testing

```bash
pytest
```

The suite takes a few seconds. Every test gets a fresh in-memory SQLite database with foreign keys switched on, since SQLite ignores them by default and would hide cascade behaviour that Postgres applies. Tests never touch the database in your `.env`. An autouse fixture makes any test that reaches the real LLM client fail immediately, so stub `app.llm.call_model` in tests that need a model reply.

## Security and known limitations

- There are no session tokens. `POST /auth/login` checks the password and returns the user, and every other endpoint trusts the `user_id` in the URL. Anyone who can reach the API can act as any user. Add token issuing to `app/routers/auth.py` and an auth dependency on the other routers before you expose this beyond a trusted network. The stored credentials are already in the right shape for it.
- Passwords are hashed with PBKDF2-HMAC-SHA256 (240,000 iterations, a salt per password, standard library only) and never returned by the API.
- `CORS_ORIGINS` defaults to `*`, which allows any origin and disables credentials. Set an explicit list in production. If a browser front end on another origin cannot reach the API, check this first: without CORS the preflight `OPTIONS` request gets a 405, the real request is never sent, and the server logs stay empty, which looks exactly like the backend being down.
- Background work (ingredient lookups and invitation delivery) runs inside the API process. If the process restarts mid-job, rows stay `pending`. Use `refresh-ingredients` and `resend-invitations` to pick them up again.
- Avatars are stored as links, not files. `avatar_url` accepts only `http://` and `https://` addresses, and there is no upload endpoint because no object storage is set up.
- Recipe suggestions wait on the LLM inside the request. Slow models push the response time up to `LLM_TIMEOUT_SECONDS`.

## License

MIT. See [LICENSE](LICENSE).
