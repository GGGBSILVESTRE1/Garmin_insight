#!/usr/bin/env python
"""
Strava OAuth + export script.

Fetches all your Strava activities and saves them to dados/strava/activities.json.
On the first run it opens the browser for OAuth authorization and saves the
refresh token to .env so future runs reuse it automatically.

Usage:
    python scripts/strava_export.py

Requirements:
    STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be in .env
    Get them at https://www.strava.com/settings/api (create an app if needed)
    Set the "Authorization Callback Domain" to: localhost
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv, set_key

from config import ENV_FILE, STRAVA_ACTIVITIES

load_dotenv(ENV_FILE)

REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPE = "activity:read_all"
OUTPUT_PATH = STRAVA_ACTIVITIES


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def _get_credentials() -> tuple[str, str]:
    client_id = os.environ.get("STRAVA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("\nERRO: STRAVA_CLIENT_ID e STRAVA_CLIENT_SECRET não encontrados no .env")
        print("      Crie um app em https://www.strava.com/settings/api")
        print("      e adicione ao .env:\n")
        print("      STRAVA_CLIENT_ID=12345")
        print("      STRAVA_CLIENT_SECRET=abc123...")
        sys.exit(1)
    return client_id, client_secret


def _exchange_code_for_tokens(client_id: str, client_secret: str, code: str) -> dict:
    import httpx
    resp = httpx.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    import httpx
    resp = httpx.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _authorize_via_browser(client_id: str) -> str:
    """Open browser for OAuth, run local server to capture the code, return it."""
    auth_url = (
        f"https://www.strava.com/oauth/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={SCOPE}"
    )

    received_code: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = parse_qs(urlparse(self.path).query)
            code = params.get("code", [""])[0]
            error = params.get("error", [""])[0]

            if error:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(f"Autorização negada: {error}".encode())
            else:
                received_code.append(code)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Autorizado!</h2>"
                    b"<p>Pode fechar esta aba e voltar ao terminal.</p></body></html>"
                )

        def log_message(self, *args):
            pass  # suppress server logs

    server = HTTPServer(("localhost", REDIRECT_PORT), _Handler)
    server.timeout = 120  # 2 min to complete OAuth

    print(f"\nAbrindo browser para autorizar o Strava...")
    print(f"Se não abrir automaticamente, acesse:\n  {auth_url}\n")
    time.sleep(1)
    webbrowser.open(auth_url)

    print("Aguardando autorização no browser...")
    while not received_code:
        server.handle_request()

    server.server_close()

    if not received_code[0]:
        print("Nenhum código recebido. Tente novamente.")
        sys.exit(1)

    return received_code[0]


class _TokenInvalid(Exception):
    pass


def get_access_token(force_refresh: bool = False, force_oauth: bool = False) -> str:
    """Return a valid access token, refreshing or doing full OAuth as needed."""
    client_id, client_secret = _get_credentials()

    refresh_token = os.environ.get("STRAVA_REFRESH_TOKEN", "").strip().strip("'\"")
    access_token = os.environ.get("STRAVA_ACCESS_TOKEN", "").strip().strip("'\"")
    expires_at_raw = os.environ.get("STRAVA_TOKEN_EXPIRES_AT", "0").strip().strip("'\"")
    try:
        expires_at = int(expires_at_raw)
    except ValueError:
        expires_at = 0

    # Use existing token only when not forcing anything and expiry is in the future
    if not force_refresh and not force_oauth and access_token and expires_at > int(time.time()) + 60:
        print("Usando token Strava existente.")
        return access_token

    # Try refresh_token unless a full re-auth is requested
    if refresh_token and not force_oauth:
        print("Renovando token Strava...")
        try:
            tokens = _refresh_access_token(client_id, client_secret, refresh_token)
            _save_tokens(tokens)
            return tokens["access_token"]
        except Exception as e:
            print(f"Falha ao renovar token: {e}. Iniciando OAuth completo...")

    # Full OAuth flow (also needed when scope was insufficient)
    print("\nO browser vai abrir para voce autorizar o acesso ao Strava.")
    print("Certifique-se de ACEITAR todas as permissoes solicitadas.\n")
    code = _authorize_via_browser(client_id)
    tokens = _exchange_code_for_tokens(client_id, client_secret, code)
    _save_tokens(tokens)
    return tokens["access_token"]


def _save_tokens(tokens: dict):
    set_key(str(ENV_FILE), "STRAVA_ACCESS_TOKEN", tokens.get("access_token", ""))
    set_key(str(ENV_FILE), "STRAVA_REFRESH_TOKEN", tokens.get("refresh_token", ""))
    set_key(str(ENV_FILE), "STRAVA_TOKEN_EXPIRES_AT", str(tokens.get("expires_at", "")))
    print("Tokens salvos em .env")


# ---------------------------------------------------------------------------
# Activity fetch
# ---------------------------------------------------------------------------

def fetch_all_activities(access_token: str) -> list[dict]:
    import httpx

    headers = {"Authorization": f"Bearer {access_token}"}
    all_activities = []
    page = 1
    per_page = 100

    print("\nBuscando atividades...")
    while True:
        resp = httpx.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers=headers,
            params={"page": page, "per_page": per_page},
            timeout=30,
        )
        if resp.status_code == 401:
            raise _TokenInvalid("Token inválido ou expirado (401)")
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_activities.extend(batch)
        print(f"  Pagina {page}: {len(batch)} atividades (total: {len(all_activities)})")
        page += 1
        time.sleep(0.5)  # respect Strava rate limit (100 req/15min)

    return all_activities


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Strava Export ===")

    access_token = get_access_token()
    try:
        activities = fetch_all_activities(access_token)
    except _TokenInvalid:
        print("Token invalido ou sem permissao. Iniciando nova autorizacao no browser...")
        # Force full OAuth (ignores cached tokens)
        access_token = get_access_token(force_refresh=True, force_oauth=True)
        activities = fetch_all_activities(access_token)

    if not activities:
        print("Nenhuma atividade encontrada.")
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    if OUTPUT_PATH.exists():
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            existing = json.load(f)

    existing_ids = {str(a.get("id")) for a in activities}
    merged = activities + [a for a in existing if str(a.get("id")) not in existing_ids]
    merged.sort(key=lambda a: a.get("start_date", ""), reverse=True)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    date_from = merged[-1].get("start_date", "?")[:10] if merged else "?"
    date_to = merged[0].get("start_date", "?")[:10] if merged else "?"
    print(f"\n{len(merged)} atividades salvas em {OUTPUT_PATH}")
    print(f"   Periodo: {date_from} a {date_to}")
    print("\nAgora rode: python cli.py")


if __name__ == "__main__":
    main()
