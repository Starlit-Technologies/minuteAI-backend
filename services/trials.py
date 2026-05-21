import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from db.base import SessionLocal
from models.user import User
from services.auth_email import send_trial_ending_notice
from services.notifications import create_notification

log = logging.getLogger(__name__)

REMINDER_LEAD_TIME = timedelta(days=3)
CHECK_INTERVAL_SECONDS = 60 * 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _format_date(value: datetime) -> str:
    return value.strftime("%d %b %Y")


def send_due_trial_reminders() -> int:
    now = _utcnow()
    reminder_cutoff = now + REMINDER_LEAD_TIME
    sent = 0

    with SessionLocal() as db:
        users = (
            db.query(User)
            .filter(
                User.trial_ends_at.isnot(None),
                User.trial_reminder_sent_at.is_(None),
                User.trial_ends_at <= reminder_cutoff,
                User.trial_ends_at > now,
                User.stripe_subscription_id.isnot(None),
            )
            .all()
        )

        for user in users:
            trial_end_label = _format_date(user.trial_ends_at)
            plan_name = (user.plan or "Pro").title()
            try:
                send_trial_ending_notice(user.email, user.name, trial_end_label, plan_name)
            except Exception as exc:
                log.warning("Failed to send trial reminder email to %s: %s", user.email, exc)

            create_notification(
                user_id=user.id,
                type="billing",
                title="Your trial ends soon",
                body=f"Your free month ends on {trial_end_label}. Your saved card will be charged automatically unless you cancel.",
                link="/billing",
            )
            user.trial_reminder_sent_at = now
            sent += 1

        if sent:
            db.commit()

    return sent


def start_trial_reminder_loop() -> None:
    def _loop():
        while True:
            try:
                count = send_due_trial_reminders()
                if count:
                    log.info("Sent %d trial reminder(s)", count)
            except Exception as exc:
                log.warning("Trial reminder loop failed: %s", exc)
            time.sleep(CHECK_INTERVAL_SECONDS)

    thread = threading.Thread(target=_loop, name="trial-reminder-loop", daemon=True)
    thread.start()
