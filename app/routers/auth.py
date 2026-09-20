"""Sign-up and log-in for the join screen.

Credentials are checked here and the user record comes back; the service does
not yet issue a session token, so the client holds the returned `id` and the
other endpoints still take it in the path. Adding tokens later is a change to
this module and a dependency on the routers — the stored credentials do not
have to change.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.database import get_session
from app.routers.users import create_user, get_by_username
from app.schemas import LoginRequest, UserCreate, UserRead
from app.security import verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def signup(payload: UserCreate, session: Session = Depends(get_session)):
    """Create an account. The same handler as POST /users, under the name the app uses."""
    return create_user(payload, session)


@router.post("/login", response_model=UserRead)
def login(payload: LoginRequest, session: Session = Depends(get_session)):
    user = get_by_username(session, payload.username)
    # One message for both failures: saying "no such User ID" would let anyone
    # enumerate who has an account.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect User ID or password")
    return user
