import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.routers import auth, feasts, friends, groceries, onboarding, recipes, users


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Schema is owned by Alembic (`alembic upgrade head`), not by startup.
    yield


app = FastAPI(
    title="Grocery Tracker",
    description="Track groceries, share a pantry with friends, and catch things before they expire.",
    version="0.1.0",
    lifespan=lifespan,
)

# Without this, a browser front end on a different origin cannot call the API
# at all: the preflight OPTIONS gets a 405 and the request never happens. The
# failure shows up as "cannot reach the backend" with nothing in the server
# logs, because the browser never sends the real request.
#
# CORS_ORIGINS is a comma-separated list; "*" (the default) allows any origin.
# Credentials are only enabled for an explicit list — the spec forbids pairing
# them with "*", and browsers reject the combination rather than falling back.
_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
_allow_any = "*" in _origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allow_any else _origins,
    allow_credentials=not _allow_any,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(groceries.router)
app.include_router(friends.router)
app.include_router(onboarding.router)
app.include_router(recipes.router)
app.include_router(feasts.router)


@app.get("/", include_in_schema=False)
def root():
    """Send the bare host to the API docs rather than a bare 404."""
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
