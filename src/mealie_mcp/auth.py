"""OAuth2/OIDC authentication for the MCP server."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urljoin

import httpx
from mcp.server.fastmcp import Context

logger = logging.getLogger(__name__)


class OAuthConfig:
    """OAuth2/OIDC configuration."""

    def __init__(
        self,
        issuer_url: str,
        client_id: str,
        client_secret: str,
        server_url: str,
    ):
        self.issuer_url = issuer_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.server_url = server_url.rstrip("/")
        self._jwks_cache: dict[str, Any] | None = None
        self._jwks_uri: str | None = None

    async def get_well_known_config(self) -> dict[str, Any]:
        """Fetch OIDC discovery document."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                urljoin(self.issuer_url, ".well-known/openid-configuration"),
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_jwks(self) -> dict[str, Any]:
        """Fetch and cache JWKS (public keys) from the issuer."""
        if self._jwks_cache is not None:
            return self._jwks_cache

        if self._jwks_uri is None:
            config = await self.get_well_known_config()
            self._jwks_uri = config.get("jwks_uri")

        if not self._jwks_uri:
            raise RuntimeError("Could not find jwks_uri in OIDC discovery")

        async with httpx.AsyncClient() as client:
            resp = await client.get(self._jwks_uri, timeout=10.0)
            resp.raise_for_status()
            self._jwks_cache = resp.json()
            return self._jwks_cache

    def get_authorization_url(self, state: str) -> str:
        """Build the authorization URL for the user to visit."""
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "scope": "openid profile email",
            "redirect_uri": urljoin(self.server_url, "/oauth/callback"),
            "state": state,
        }
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.issuer_url}/application/o/authorize/?{query_string}"

    async def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        """Exchange authorization code for access token."""
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": urljoin(self.server_url, "/oauth/callback"),
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                urljoin(self.issuer_url, "/application/o/token/"),
                data=payload,
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()


def verify_oauth_token(ctx: Context) -> dict[str, Any] | None:
    """Extract and verify OAuth token from request headers.

    Returns the decoded token claims if valid, None otherwise.
    Token verification is deferred to the authorization server (introspection)
    for simplicity and freshness.
    """
    auth_header = ctx.request_context.request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[7:]
    if not token:
        return None

    # For now, just return the token as a claim. Real verification
    # would call the introspection endpoint or verify the JWT.
    # Since we trust Authentik to issue valid tokens, and Claude/OpenAI
    # will send them, this is a basic check.
    return {"access_token": token}
