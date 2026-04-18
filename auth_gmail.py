"""Run once to produce token.json from credentials.json."""
from google_auth_oauthlib.flow import InstalledAppFlow
import json, pathlib

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def main():
    creds_path = pathlib.Path(__file__).parent / "credentials.json"
    if not creds_path.exists():
        raise FileNotFoundError("credentials.json not found — place it next to this script")

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(port=0)

    token_path = pathlib.Path(__file__).parent / "token.json"
    token_path.write_text(creds.to_json())
    print(f"token.json saved to {token_path}")

if __name__ == "__main__":
    main()
