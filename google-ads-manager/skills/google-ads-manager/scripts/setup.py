#!/usr/bin/env python3
"""
Google Ads credentials setup script.
Reads credentials from stdin as KEY=VALUE lines and saves to .env file.
"""

import os
import sys
import json
from pathlib import Path

ALLOWED_KEYS = {
    'GOOGLE_ADS_CLIENT_ID',
    'GOOGLE_ADS_CLIENT_SECRET',
    'GOOGLE_ADS_REFRESH_TOKEN',
    'GOOGLE_ADS_DEVELOPER_TOKEN',
    'GOOGLE_ADS_CUSTOMER_ID',
    'GOOGLE_ADS_LOGIN_CUSTOMER_ID',
}


def extract_from_token_file(token_path: str) -> dict:
    """Extract OAuth credentials from an existing google_ads_token.json file."""
    with open(token_path, 'r') as f:
        data = json.load(f)
    extracted = {}
    if data.get('client_id'):
        extracted['GOOGLE_ADS_CLIENT_ID'] = data['client_id']
    if data.get('client_secret'):
        extracted['GOOGLE_ADS_CLIENT_SECRET'] = data['client_secret']
    if data.get('refresh_token'):
        extracted['GOOGLE_ADS_REFRESH_TOKEN'] = data['refresh_token']
    return extracted


def setup_credentials(project_dir: str, token_file: str = None):
    env_file = Path(project_dir) / '.env'
    credentials = {}

    if token_file:
        try:
            credentials.update(extract_from_token_file(token_file))
            print(f"Extracted credentials from {token_file}")
        except Exception as e:
            print(f"Warning: could not read token file: {e}")

    print("Enter additional credentials as KEY=VALUE (one per line, Ctrl+D when done):")
    for line in sys.stdin:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()
            if key in ALLOWED_KEYS and value:
                credentials[key] = value

    if not credentials:
        print("No credentials provided.")
        sys.exit(1)

    existing = {}
    if env_file.exists():
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    if k.strip() not in ALLOWED_KEYS:
                        existing[k.strip()] = v.strip()

    with open(env_file, 'w') as f:
        for k, v in existing.items():
            f.write(f"{k}={v}\n")
        for k, v in credentials.items():
            f.write(f"{k}={v}\n")

    os.chmod(env_file, 0o600)

    print(f"\nCredentials saved to {env_file}")
    for k, v in credentials.items():
        masked = v[:10] + "..." if len(v) > 10 else v
        print(f"  {k}: {masked}")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Setup Google Ads credentials')
    parser.add_argument('project_dir', help='Directory to write .env file')
    parser.add_argument('--token-file', help='Path to existing google_ads_token.json to extract credentials from')
    args = parser.parse_args()
    setup_credentials(args.project_dir, args.token_file)
