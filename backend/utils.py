from google.oauth2.credentials import Credentials

def load_access_token_from_file(path="token.json") -> str:
    creds = Credentials.from_authorized_user_file(path)
    # Refresh if expired
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
    return creds.token