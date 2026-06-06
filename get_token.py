from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json",
    SCOPES
)

creds = flow.run_local_server(port=0)

print("\n=== COPY TO RENDER ENV ===\n")

print("GOOGLE_OAUTH_CLIENT_ID=")
print(creds.client_id)

print("\nGOOGLE_OAUTH_CLIENT_SECRET=")
print(creds.client_secret)

print("\nGOOGLE_OAUTH_REFRESH_TOKEN=")
print(creds.refresh_token)