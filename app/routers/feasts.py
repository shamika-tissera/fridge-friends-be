"""Feasts: a chosen recipe, the people eating it, and their invitations."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlmodel import Session, col, select

from app import database
from app.database import get_session
from app.dietary import rules_for
from app.models import (
    DeliveryStatus,
    Feast,
    FeastAttendee,
    InviteResponse,
    Notification,
    User,
)
from app.notifications import format_invitation, notify_feast_invitations
from app.routers.users import get_user_or_404
# Aliased: this module's own endpoint is also called create_feast.
from app.services import create_feast as create_feast_record
from app.schemas import (
    AttendeeRead,
    FeastCreate,
    FeastRead,
    InviteResponseUpdate,
    NotificationRead,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["feasts"])


def dispatch_invitations(feast_id: int) -> None:
    """Background delivery. Opens its own session; never raises into the request."""
    # database.engine is read at call time so tests can swap the engine.
    with Session(database.engine) as session:
        try:
            notify_feast_invitations(session, feast_id)
        except Exception:  # pragma: no cover - last-resort guard
            logger.exception("invitation dispatch failed for feast %s", feast_id)


def to_read(session: Session, feast: Feast) -> FeastRead:
    rows = session.exec(
        select(FeastAttendee, User)
        .join(User, col(FeastAttendee.user_id) == col(User.id))
        .where(col(FeastAttendee.feast_id) == feast.id)
        .order_by(col(FeastAttendee.id).asc())
    ).all()
    notes = session.exec(
        select(Notification).where(col(Notification.feast_id) == feast.id)
    ).all()
    sent = [n for n in notes if n.delivery_status == DeliveryStatus.sent]
    pending = [n for n in notes if n.delivery_status != DeliveryStatus.sent]
    host = session.get(User, feast.host_id)
    return FeastRead(
        id=feast.id,
        name=feast.name,
        host_id=feast.host_id,
        host_name=host.name if host else "",
        scheduled_for=feast.scheduled_for,
        recipe=feast.recipe,
        attendees=[
            AttendeeRead(
                user_id=user.id,
                username=user.username,
                name=user.name,
                email=user.email,
                response=row.response,
                responded_at=row.responded_at,
                is_host=user.id == feast.host_id,
            )
            for row, user in rows
        ],
        invitations_sent=len(sent),
        invitations_pending=len(pending),
        created_at=feast.created_at,
    )


def get_feast_or_404(feast_id: int, session: Session) -> Feast:
    feast = session.get(Feast, feast_id)
    if feast is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Feast {feast_id} not found")
    return feast


@router.post("/feasts", response_model=FeastRead, status_code=status.HTTP_201_CREATED)
def create_feast(
    payload: FeastCreate,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Turn a chosen recipe into a feast and invite everyone.

    The recipe is stored as a snapshot rather than a reference: suggestions are
    generated on demand and persisted nowhere, and the feast should keep
    recording what was agreed even after pantries change.
    """
    host = get_user_or_404(payload.host_id, session)

    # The host attends their own feast, and is already committed to it.
    attendee_ids = list(dict.fromkeys([payload.host_id, *payload.attendee_ids]))
    for uid in attendee_ids:
        get_user_or_404(uid, session)   # 404 before anything is written

    # Re-check the recipe against everyone who will eat it. The suggestion was
    # filtered for whoever it was generated for, and this guest list can be a
    # different set — or the same one after somebody added an allergy.
    rules = rules_for(session, attendee_ids)
    violations = rules.violations(
        [payload.recipe.name]
        + [ingredient.name for ingredient in payload.recipe.uses]
        + payload.recipe.missing
    )
    if violations:
        raise HTTPException(
            422,   # the constant was renamed between Starlette versions
            "This recipe clashes with an attendee's allergies or diet: "
            + ", ".join(sorted(set(violations))),
        )

    feast = create_feast_record(
        session,
        name=payload.name,
        host=host,
        attendee_ids=attendee_ids,
        recipe=payload.recipe.model_dump(mode="json"),
        scheduled_for=payload.scheduled_for,
    )

    # Delivery happens after the response: a slow or broken channel must not
    # stop the feast being created.
    background.add_task(dispatch_invitations, feast.id)
    return to_read(session, feast)


@router.get("/feasts/{feast_id}", response_model=FeastRead)
def get_feast(feast_id: int, session: Session = Depends(get_session)):
    return to_read(session, get_feast_or_404(feast_id, session))


@router.get("/users/{user_id}/feasts", response_model=list[FeastRead])
def list_feasts_for_user(
    user_id: int,
    hosting_only: bool = False,
    session: Session = Depends(get_session),
):
    """Every feast this user hosts or is invited to, newest first."""
    get_user_or_404(user_id, session)
    if hosting_only:
        statement = select(Feast).where(col(Feast.host_id) == user_id)
    else:
        feast_ids = session.exec(
            select(FeastAttendee.feast_id).where(col(FeastAttendee.user_id) == user_id)
        ).all()
        if not feast_ids:
            return []
        statement = select(Feast).where(col(Feast.id).in_(feast_ids))
    feasts = session.exec(statement.order_by(col(Feast.id).desc())).all()
    return [to_read(session, f) for f in feasts]


@router.post("/feasts/{feast_id}/respond/{user_id}", response_model=FeastRead)
def respond_to_invitation(
    feast_id: int,
    user_id: int,
    payload: InviteResponseUpdate,
    session: Session = Depends(get_session),
):
    """Accept or decline an invitation."""
    feast = get_feast_or_404(feast_id, session)
    attendee = session.exec(
        select(FeastAttendee)
        .where(col(FeastAttendee.feast_id) == feast_id)
        .where(col(FeastAttendee.user_id) == user_id)
    ).first()
    if attendee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That user was not invited")

    attendee.response = payload.response
    attendee.responded_at = datetime.now(timezone.utc)
    session.add(attendee)
    session.commit()
    return to_read(session, feast)


@router.post("/feasts/{feast_id}/resend-invitations", response_model=FeastRead)
def resend_invitations(feast_id: int, session: Session = Depends(get_session)):
    """Retry any invitation that has not been delivered."""
    feast = get_feast_or_404(feast_id, session)
    notify_feast_invitations(session, feast_id)
    session.refresh(feast)
    return to_read(session, feast)


@router.get("/users/{user_id}/notifications", response_model=list[NotificationRead])
def list_notifications(
    user_id: int,
    unread_only: bool = Query(False),
    session: Session = Depends(get_session),
):
    """This user's inbox, newest first."""
    get_user_or_404(user_id, session)
    statement = select(Notification).where(col(Notification.user_id) == user_id)
    if unread_only:
        statement = statement.where(col(Notification.read_at).is_(None))
    return session.exec(statement.order_by(col(Notification.id).desc())).all()


@router.post("/notifications/{notification_id}/read", response_model=NotificationRead)
def mark_notification_read(
    notification_id: int, session: Session = Depends(get_session)
):
    notification = session.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        session.add(notification)
        session.commit()
        session.refresh(notification)
    return notification
