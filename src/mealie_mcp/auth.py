"""OAuth2/OIDC authentication for the MCP server."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx
import jwt
from jwt import PyJWKClient
from mcp.server.fastmcp import Context

logger = logging.getLogger(__name__)

# Cache the OIDC discovery document for this many seconds before refetching.
_DISCOVERY_TTL_SECONDS = 3600


class OAuthConfig:
    """OAuth2/OIDC configuration."""

    def __init__(
        self,
        issuer_url: str,
        client_id: str,
        client_secret: str,
        server_url: str,
        jwks_uri: str | None = None,
    ):
        self.issuer_url = issuer_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.server_url = server_url.rstrip("/")
        self._discovery_doc: dict[str, Any] | None = None
        self._discovery_fetched_at: float = 0.0
        self._jwks_uri_override = jwks_uri
        self._jwks_client: PyJWKClient | None = None

    async def get_well_known_config(self) -> dict[str, Any]:
        """Fetch (and cache) the OIDC discovery document."""
        if (
            self._discovery_doc is not None
            and (time.monotonic() - self._discovery_fetched_at) < _DISCOVERY_TTL_SECONDS
        ):
            return self._discovery_doc

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                urljoin(self.issuer_url + "/", ".well-known/openid-configuration"),
                timeout=10.0,
            )
            resp.raise_for_status()
            self._discovery_doc = resp.json()
            self._discovery_fetched_at = time.monotonic()
            return self._discovery_doc

    async def _resolve_jwks_uri(self) -> str:
        if self._jwks_uri_override:
            return self._jwks_uri_override
        config = await self.get_well_known_config()
        jwks_uri = config.get("jwks_uri")
        if not jwks_uri:
            raise RuntimeError("Could not find jwks_uri in OIDC discovery")
        return jwks_uri

    async def get_jwks_client(self) -> PyJWKClient:
        """Return a cached PyJWKClient for the issuer's JWKS endpoint."""
        if self._jwks_client is None:
            jwks_uri = await self._resolve_jwks_uri()
            # PyJWKClient handles its own internal caching of fetched keys.
            self._jwks_client = PyJWKClient(jwks_uri, cache_keys=True, lifespan=3600)
        return self._jwks_client

    async def get_authorization_url(self, state: str) -> str:
        """Build the authorization URL using the discovered authorization_endpoint."""
        config = await self.get_well_known_config()
        authorization_endpoint = config.get("authorization_endpoint")
        if not authorization_endpoint:
            raise RuntimeError("Could not find authorization_endpoint in OIDC discovery")

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "scope": "openid profile email",
            "redirect_uri": urljoin(self.server_url + "/", "oauth/callback"),
            "state": state,
        }
        return f"{authorization_endpoint}?{urlencode(params)}"

    async def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        """Exchange authorization code for access token using the discovered token endpoint."""
        config = await self.get_well_known_config()
        token_endpoint = config.get("token_endpoint")
        if not token_endpoint:
            raise RuntimeError("Could not find token_endpoint in OIDC discovery")

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": urljoin(self.server_url + "/", "oauth/callback"),
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(token_endpoint, data=payload, timeout=10.0)
            resp.raise_for_status()
            return resp.json()


async def verify_oauth_token(token: str, oauth_config: OAuthConfig) -> dict[str, Any] | None:
    """Verify a JWT bearer token against the issuer's JWKS.

    Returns the decoded token claims if valid, None otherwise.
    """
    if not token:
        return None

    try:
        jwks_client = await oauth_config.get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=oauth_config.client_id,
        )
        return claims
    except Exception as exc:
        logger.warning("OAuth token verification failed: %s", exc)
        return None


def extract_bearer_token(ctx: Context) -> str | None:
    """Pull a Bearer token from the request's Authorization header."""
    try:
        auth_header = ctx.request_context.request.headers.get("authorization", "")
    except AttributeError:
        return None
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:].strip()
    return token or None
