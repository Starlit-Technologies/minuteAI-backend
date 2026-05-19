import os
from pathlib import Path
import requests


_DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"

OUTPUT_DIR = Path(__file__).parent.parent.parent / "storage" / "outputs"


def upload_to_drive(access_token: str, settings: dict, job_id: str, minutes: dict, app_url: str) -> None:
    """Upload the meeting DOCX output to the user's Google Drive."""
    docx_path = OUTPUT_DIR / f"{job_id}.docx"
    if not docx_path.exists():
        return

    title = minutes.get("title", "Meeting")
    filename = f"{title} - Minutes.docx"

    metadata: dict = {
        "name": filename,
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    folder_id = settings.get("folder_id")
    if folder_id:
        metadata["parents"] = [folder_id]

    with open(docx_path, "rb") as f:
        file_content = f.read()

    # Multipart upload
    import json
    boundary = "boundary_minuteai"
    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n"
    ).encode("utf-8") + file_content + f"\r\n--{boundary}--".encode("utf-8")

    requests.post(
        f"{_DRIVE_UPLOAD_URL}?uploadType=multipart",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        data=body,
        timeout=30,
    )


def list_folders(access_token: str) -> list[dict]:
    """Return a list of Google Drive folders the user can select as upload destination."""
    resp = requests.get(
        _DRIVE_FILES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "q": "mimeType='application/vnd.google-apps.folder' and trashed=false",
            "fields": "files(id,name)",
            "pageSize": 100,
            "orderBy": "name",
        },
        timeout=10,
    )
    if not resp.ok:
        return []
    return [{"id": f["id"], "name": f["name"]} for f in resp.json().get("files", [])]
