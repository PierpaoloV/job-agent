"""Run once to produce token.json from credentials.json."""

import argparse
import os
import pathlib
import sys

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from gmail_auth import (
    SCOPES,
    github_secret_set_command,
    resolve_credentials_path,
    resolve_token_output_path,
    update_github_token_secret,
    verify_dedicated_mailbox,
)

def main(
    credentials: str | None = None,
    token: str | None = None,
    sync_secret: bool = False,
    repo: str | None = None,
    career_email: str | None = None,
) -> None:
    creds_path = resolve_credentials_path(credentials)

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    service = build("gmail", "v1", credentials=creds)
    authenticated = verify_dedicated_mailbox(
        service,
        creds,
        expected_email=career_email,
    )

    token_path = resolve_token_output_path(token)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    print(f"Authorized dedicated Gmail account: {authenticated}")
    print(f"token.json saved to {token_path}")

    if sync_secret:
        if not repo:
            raise ValueError(
                "--repo or JOB_AGENT_GITHUB_REPOSITORY is required with --sync-secret"
            )
        update_github_token_secret(repo, token_path)
        print(f"Updated GMAIL_TOKEN_JSON in {repo}")
    elif repo:
        gh_command = " ".join(github_secret_set_command(repo))
        print(f"Update GitHub Actions by piping {token_path} to: {gh_command}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", help="Path to the Google OAuth desktop client JSON")
    parser.add_argument("--token", help="Path to write the refreshed Gmail token JSON")
    parser.add_argument(
        "--sync-secret",
        action="store_true",
        help="Also update the GMAIL_TOKEN_JSON GitHub Actions secret via gh",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("JOB_AGENT_GITHUB_REPOSITORY"),
        help="GitHub repo to update when --sync-secret is used",
    )
    parser.add_argument(
        "--career-email",
        default=os.environ.get("JOB_AGENT_CAREER_GMAIL"),
        help="Dedicated Gmail identity to authorize",
    )
    args = parser.parse_args()
    main(
        credentials=args.credentials,
        token=args.token,
        sync_secret=args.sync_secret,
        repo=args.repo,
        career_email=args.career_email,
    )
