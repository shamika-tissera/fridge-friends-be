"""Delivering notifications.

Every notification is written to the `notification` table first — that row is
the user's in-app inbox and the durable record. A *channel* then optionally
pushes it somewhere external (email, push, SMS).

No external provider is configured in this project, so the default channel logs
and marks the row sent. Swap in a real channel by setting NOTIFICATION_CHANNEL
and adding it to CHANNELS below; nothing else has to change.
"""

import logging
import os
from datetime import datetime, timezone

from sqlmodel import Session, col, select

from app.models import DeliveryStatus, Feast, Notification, User

logger = logging.getLogger(__name__)


class DeliveryFailed(RuntimeError):
    """A channel could not deliver. The row stays `failed` and is retryable."""


class LogChannel:
    """Writes the invitation to the application log. The default."""

    name = "log"

    def send(self, *, to: User, title: str, body: str) -> None:
        logger.info("NOTIFY %s <%s>: %s | %s", to.name, to.email, title, body)


class NullChannel:
    """Delivers nothing. For tests and for running without notifications."""

    name = "null"

    def send(self, *, to: User, title: str, body: str) -> None:
        return None


CHANNELS = {"log": LogChannel, "null": NullChannel}


def get_channel():
    name = os.getenv("NOTIFICATION_CHANNEL", "log")
    channel = CHANNELS.get(name)
    if channel is None:
        logger.warning("unknown NOTIFICATION_CHANNEL %r, falling back to log", name)
        channel = LogChannel
    return channel()


def format_invitation(feast: Feast, host: User) -> tuple[str, str]:
    """The wording of a feast invitation."""
    recipe = feast.recipe or {}
    dish = recipe.get("name", "something good")
    title = f"{host.name} invited you to {feast.name}"

    lines = [f"{host.name} is cooking {dish}."]
    if feast.scheduled_for is not None:
        lines.append(f"When: {feast.scheduled_for:%a %d %b, %H:%M}.")
    total = recipe.get("total_minutes")
    if total:
        lines.append(f"Takes about {total} minutes.")

    # Tell each attendee what they are expected to bring — the recipe snapshot
    # already records whose kitchen each ingredient is in.
    by_user: dict[str, list[str]] = {}
    for ingredient in recipe.get("uses", []):
        for owner in ingredient.get("from_users", []):
            by_user.setdefault(owner, []).append(ingredient.get("name", ""))
    if by_user:
        lines.append(
            "Bringing: "
            + "; ".join(f"{who} — {', '.join(items)}" for who, items in by_user.items())
            + "."
        )
    missing = recipe.get("missing") or []
    if missing:
        lines.append(f"Still to buy: {', '.join(missing)}.")

    return title, " ".join(lines)


def notify_feast_invitations(session: Session, feast_id: int) -> int:
    """Send every pending invitation for a feast. Returns how many were sent.

    Safe to call again: only rows still `pending` or `failed` are retried, so a
    partial failure can be re-run without double-inviting anyone.
    """
    feast = session.get(Feast, feast_id)
    if feast is None:
        return 0

    pending = session.exec(
        select(Notification, User)
        .join(User, col(Notification.user_id) == col(User.id))
        .where(col(Notification.feast_id) == feast_id)
        .where(col(Notification.delivery_status) != DeliveryStatus.sent)
    ).all()

    channel = get_channel()
    sent = 0
    for notification, user in pending:
        try:
            channel.send(to=user, title=notification.title, body=notification.body)
        except Exception as exc:  # a channel is third-party; never let it 500 the caller
            logger.warning("notification %s failed: %s", notification.id, exc)
            notification.delivery_status = DeliveryStatus.failed
            notification.delivery_error = str(exc)[:255]
        else:
            notification.delivery_status = DeliveryStatus.sent
            notification.delivery_error = None
            sent += 1
        session.add(notification)
    session.commit()
    logger.info("feast %s: %d/%d invitations sent via %s",
                feast_id, sent, len(pending), channel.name)
    return sent
