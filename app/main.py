from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.routers import friends, groceries, onboarding, users


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

app.include_router(users.router)
app.include_router(groceries.router)
app.include_router(friends.router)
app.include_router(onboarding.router)


@app.get("/", include_in_schema=False)
def root():
    """Send the bare host to the API docs rather than a bare 404."""
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
