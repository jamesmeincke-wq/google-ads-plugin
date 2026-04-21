# Google Ads API Credentials Setup

This command saves Google Ads API credentials to a `.env` file that persists across sessions.

## Credentials Required

Five values are needed:

- `GOOGLE_ADS_CLIENT_ID` — OAuth client ID from Google Cloud Console
- `GOOGLE_ADS_CLIENT_SECRET` — OAuth client secret from Google Cloud Console
- `GOOGLE_ADS_REFRESH_TOKEN` — Long-lived refresh token from your OAuth flow
- `GOOGLE_ADS_DEVELOPER_TOKEN` — From Google Ads → Tools → API Center → Developer Token
- `GOOGLE_ADS_CUSTOMER_ID` — Your 10-digit Google Ads account ID (no dashes)

Two optional values:
- `GOOGLE_ADS_LOGIN_CUSTOMER_ID` — Only needed if using a manager (MCC) account

## Where to Find Each Value

**Existing users with google_ads_token.json:**
The file already contains `client_id`, `client_secret`, and `refresh_token` fields.
Copy those values directly. You still need to add `GOOGLE_ADS_DEVELOPER_TOKEN` and `GOOGLE_ADS_CUSTOMER_ID` separately.

**New users:**
1. Go to [Google Cloud Console](https://console.cloud.google.com) → Credentials → your OAuth 2.0 Client ID to get `client_id` and `client_secret`
2. Run an OAuth flow to get a `refresh_token` (use the existing `get_oauth_credentials()` flow or OAuth Playground)
3. Go to Google Ads → click the wrench icon → API Center to find your `developer_token`
4. Your Customer ID is the 10-digit number shown in the top-right of Google Ads (remove dashes)

## Setup Process

Locate the project directory, then pipe credentials to the setup script:

```
echo "GOOGLE_ADS_CLIENT_ID=your_client_id
GOOGLE_ADS_CLIENT_SECRET=your_secret
GOOGLE_ADS_REFRESH_TOKEN=your_refresh_token
GOOGLE_ADS_DEVELOPER_TOKEN=your_dev_token
GOOGLE_ADS_CUSTOMER_ID=5157364662" | python3 ${CLAUDE_PLUGIN_ROOT}/skills/google-ads-manager/scripts/setup.py .
```

After setup, verify by running: `google-ads list-accounts`

## Security

Credentials are piped through stdin (never exposed in shell history). The `.env` file is saved with `chmod 0o600` (owner read-only). Never commit `.env` to git.
