import uuid
from models.database import SessionLocal, NotificationRecord


def create_notification(
    user_id: str,
    type: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
) -> None:
    try:
        with SessionLocal() as session:
            session.add(NotificationRecord(
                id=str(uuid.uuid4()),
                user_id=user_id,
                type=type,
                title=title,
                body=body,
                link=link,
                read=False,
            ))
            session.commit()
    except Exception:
        pass
