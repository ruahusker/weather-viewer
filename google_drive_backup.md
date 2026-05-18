# Google Drive Backup

This uses the official Google Drive API to upload a single archive of the
local restic repository from a headless Linux server.

## What it uploads

- Local restic repo: `/home/main/backups/weather-viewer`
- Uploaded as one file:
  - `weather-viewer-restic-repo.tar.gz`

This is intentional. Uploading the restic repo as a single archive is simpler
and safer than trying to sync the repository tree file-by-file to Drive.

## 1. Create Google OAuth credentials

In Google Cloud Console:

1. Create or choose a project
2. Enable the Google Drive API
3. Create an OAuth client ID
4. Choose `Desktop app`
5. Download the client JSON

Place it on the server at:

```bash
mkdir -p /home/main/.config/google-drive
cp client_secret.json /home/main/.config/google-drive/client_secret.json
chmod 600 /home/main/.config/google-drive/client_secret.json
```

## 2. Install Google API packages

Use the project virtualenv:

```bash
cd /home/main/weather-viewer
.venv/bin/pip install google-api-python-client google-auth google-auth-oauthlib
```

## 3. Create a Drive folder

In Google Drive, create a folder for the backup archive.

Copy the folder ID from the URL:

```text
https://drive.google.com/drive/folders/FOLDER_ID_HERE
```

## 4. First-time auth on a headless server

Use SSH port forwarding from your local machine:

```bash
ssh -L 8765:127.0.0.1:8765 wx@YOUR_SERVER
```

Then on the server, run:

```bash
cd /home/main/weather-viewer
.venv/bin/python scripts/upload_restic_repo_to_drive.py \
  --drive-folder-id YOUR_DRIVE_FOLDER_ID
```

The script will print a Google authorization URL.

Open that URL in your local browser, sign in, approve access, and the token
will be saved at:

```text
/home/main/.config/google-drive/token.json
```

## 5. Subsequent uploads

After the token exists, just run:

```bash
cd /home/main/weather-viewer
.venv/bin/python scripts/upload_restic_repo_to_drive.py \
  --drive-folder-id YOUR_DRIVE_FOLDER_ID
```

## 6. Suggested automation

Run local restic backup first, then upload the resulting repo archive:

```bash
cd /home/main/weather-viewer
bash backup.sh
.venv/bin/python scripts/upload_restic_repo_to_drive.py \
  --drive-folder-id YOUR_DRIVE_FOLDER_ID
```

## Restore

1. Download `weather-viewer-restic-repo.tar.gz` from Google Drive
2. Extract it back to `/home/main/backups/`
3. Use restic against the restored repo

Example:

```bash
mkdir -p /home/main/backups
tar -xzf weather-viewer-restic-repo.tar.gz -C /home/main/backups
export RESTIC_PASSWORD_FILE=/home/main/.config/restic/password.txt
/home/main/.local/bin/restic snapshots --repo /home/main/backups/weather-viewer
```
