# pip install google-auth google-auth-oauthlib google-api-python-client

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# One-time OAuth flow — saves token.json locally
flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
creds = flow.run_local_server(port=0)

# Save it — you only do this once
with open('token.json', 'w') as f:
    f.write(creds.to_json())

# Every subsequent run — load from file
creds = Credentials.from_authorized_user_file('token.json', SCOPES)
service = build('sheets', 'v4', credentials=creds)

# Now call the API directly
result = service.spreadsheets().values().get(
    spreadsheetId="1JHi-tN1RpnbDgR7dQ3gM0hYnRj4Gmab1_9Fy9EF46gg",
    range='Model!B8:D8'
).execute()
