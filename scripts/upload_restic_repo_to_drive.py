#!/usr/bin/env python3
"""Upload the local weather-viewer restic repo to Google Drive.

This is intended for headless Linux use with official Google APIs only.

Workflow:
1. Create a compressed archive of the local restic repository.
2. Upload or update a single file in a chosen Google Drive folder.

Auth:
- Uses Google OAuth installed-app flow.
- On a headless server, use SSH port forwarding to complete the local-server
  OAuth flow from your desktop browser.
"""

from __future__ import annotations

import argparse
import os
import tarfile
import tempfile
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive and upload a local restic repo to Google Drive."
    )
    parser.add_argument(
        "--repo-path",
        default="/home/main/backups/weather-viewer",
        help="Path to the local restic repository directory",
    )
    parser.add_argument(
        "--drive-folder-id",
        required=True,
        help="Google Drive folder ID that will hold the uploaded archive",
    )
    parser.add_argument(
        "--credentials",
        default="/home/main/.config/google-drive/client_secret.json",
        help="OAuth client credentials JSON from Google Cloud Console",
    )
    parser.add_argument(
        "--token",
        default="/home/main/.config/google-drive/token.json",
        help="Path to store the reusable OAuth token JSON",
    )
    parser.add_argument(
        "--archive-name",
        default="weather-viewer-restic-repo.tar.gz",
        help="Filename to use in Google Drive",
    )
    parser.add_argument(
        "--auth-port",
        type=int,
        default=8765,
        help="Local OAuth callback port for headless SSH forwarding",
    )
    return parser.parse_args()


def load_credentials(credentials_path: Path, token_path: Path, auth_port: int) -> Credentials:
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if creds and creds.valid:
        return creds

    if not credentials_path.exists():
        raise FileNotFoundError(
            f"Missing OAuth client credentials file: {credentials_path}"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(
        host="127.0.0.1",
        port=auth_port,
        open_browser=False,
        authorization_prompt_message=(
            "Open this URL in your local browser to authorize Google Drive access:\n"
            "{url}\n"
        ),
        success_message="Authorization complete. You can close this window.",
    )
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def create_archive(repo_path: Path, archive_path: Path) -> None:
    if not repo_path.is_dir():
        raise FileNotFoundError(f"Restic repository directory not found: {repo_path}")

    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(repo_path, arcname=repo_path.name)


def find_existing_file(service, folder_id: str, filename: str) -> str | None:
    escaped_name = filename.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{escaped_name}' and "
        f"'{folder_id}' in parents and trashed = false"
    )
    response = (
        service.files()
        .list(
            q=query,
            spaces="drive",
            fields="files(id, name)",
            pageSize=10,
        )
        .execute()
    )
    files = response.get("files", [])
    if not files:
        return None
    return files[0]["id"]


def upload_archive(service, folder_id: str, archive_path: Path, archive_name: str) -> str:
    media = MediaFileUpload(str(archive_path), mimetype="application/gzip", resumable=True)
    existing_id = find_existing_file(service, folder_id, archive_name)
    metadata = {"name": archive_name, "parents": [folder_id]}

    if existing_id:
        file = (
            service.files()
            .update(
                fileId=existing_id,
                media_body=media,
                body={"name": archive_name},
                fields="id, name, size, modifiedTime",
            )
            .execute()
        )
    else:
        file = (
            service.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id, name, size, modifiedTime",
            )
            .execute()
        )
    return file["id"]


def main() -> int:
    args = parse_args()
    repo_path = Path(args.repo_path)
    credentials_path = Path(args.credentials)
    token_path = Path(args.token)

    creds = load_credentials(credentials_path, token_path, args.auth_port)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    with tempfile.TemporaryDirectory(prefix="wx-restic-drive-") as temp_dir:
        archive_path = Path(temp_dir) / args.archive_name
        print(f"Creating archive from {repo_path} ...")
        create_archive(repo_path, archive_path)
        size_mb = archive_path.stat().st_size / (1024 * 1024)
        print(f"Archive ready: {archive_path.name} ({size_mb:.2f} MiB)")

        file_id = upload_archive(
            service,
            args.drive_folder_id,
            archive_path,
            args.archive_name,
        )
        print(f"Upload complete. Drive file id: {file_id}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
