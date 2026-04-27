#!/usr/bin/env python3
"""Google Ads REST API client with .env credential loading."""

import os
import json
import requests
from pathlib import Path
from typing import Optional, List, Dict

API_VERSION = "v23"
BASE_URL = "https://googleads.googleapis.com"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def format_customer_id(customer_id: str) -> str:
    return ''.join(c for c in str(customer_id) if c.isdigit()).zfill(10)


class GoogleAdsClient:
    def __init__(self):
        self._token = None
        self._token_expiry = None
        self.client_id = self._load("GOOGLE_ADS_CLIENT_ID")
        self.client_secret = self._load("GOOGLE_ADS_CLIENT_SECRET")
        self.refresh_token = self._load("GOOGLE_ADS_REFRESH_TOKEN")
        self.developer_token = self._load("GOOGLE_ADS_DEVELOPER_TOKEN")
        self.default_customer_id = self._load("GOOGLE_ADS_CUSTOMER_ID", required=False)
        self.login_customer_id = self._load("GOOGLE_ADS_LOGIN_CUSTOMER_ID", required=False)

    def _load(self, key: str, required: bool = True) -> Optional[str]:
        val = os.environ.get(key)
        if val:
            return val

        search_paths = [
            Path('.env'),
            Path.cwd() / '.env',
            Path(__file__).parent.parent.parent.parent / '.env',
            Path(__file__).parent.parent / '.env',
            Path(__file__).parent / '.env',
        ]

        seen = set()
        for env_path in search_paths:
            resolved = str(env_path.resolve()) if env_path.exists() else str(env_path)
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                if env_path.exists():
                    with open(env_path, 'r') as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith(key + '='):
                                return line.split('=', 1)[1].strip()
            except (OSError, PermissionError):
                continue

        if required:
            raise ValueError(
                f"Missing credential: {key}\n"
                "Run /google-ads-setup to configure your credentials."
            )
        return None

    def get_access_token(self) -> str:
        import time
        now = time.time()
        if self._token and self._token_expiry and now < self._token_expiry - 60:
            return self._token
        resp = requests.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }, timeout=30)
        if resp.status_code != 200:
            raise ValueError(f"Token refresh failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = now + data.get("expires_in", 3600)
        return self._token

    def get_headers(self, customer_id: Optional[str] = None) -> Dict[str, str]:
        token = self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "developer-token": self.developer_token,
            "content-type": "application/json",
        }
        login_id = self.login_customer_id or customer_id
        if login_id:
            headers["login-customer-id"] = format_customer_id(login_id)
        return headers

    def search(self, customer_id: str, query: str):
        fid = format_customer_id(customer_id)
        headers = self.get_headers(fid)
        url = f"{BASE_URL}/{API_VERSION}/customers/{fid}/googleAds:search"
        resp = requests.post(url, headers=headers, json={"query": query}, timeout=60)
        return fid, resp

    def mutate(self, customer_id: str, operations: List[dict]):
        fid = format_customer_id(customer_id)
        headers = self.get_headers(fid)
        url = f"{BASE_URL}/{API_VERSION}/customers/{fid}/googleAds:mutate"
        resp = requests.post(url, headers=headers, json={"mutateOperations": operations}, timeout=60)
        return fid, resp

    def list_accessible_customers(self):
        headers = self.get_headers()
        url = f"{BASE_URL}/{API_VERSION}/customers:listAccessibleCustomers"
        resp = requests.get(url, headers=headers, timeout=30)
        return resp
