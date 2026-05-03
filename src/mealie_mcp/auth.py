"""OAuth2/OIDC authentication for the MCP server."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urljoin

import httpx
import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)


class OAuthConfig:
    """OAuth2/OIDC configuration.

    The MCP server is a protected resource: it does not run the auth-code
    flow itself. ChatGPT (or any MCP client) discovers Authentik from
    /.well-known/oauth-protected-resource and performs the flow directly.
    This class exists to (a) hold the issuer/client_id used for token
    validation and discovery advertising, and (b) verify Bearer tokens
    against Authentik's JWKS.
    """

    def __init__(
        self,
        issuer_url: str,
        client_id: str,
        server_url: str,
        client_secret: str | None = None,
    ):
        self.issuer_url = issuer_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.server_url = server_url.rstrip("/")
        self._oidc_config: dict[str, Any] | None = None
        self._jwk_client: PyJWKClient | None = None

    async def get_well_known_config(self) -> dict[str, Any]:
        """Fetch and cache the OIDC discovery document."""
        if self._oidc_config is not None:
            return self._oidc_config
        url = urljoin(self.issuer_url + "/", ".well-known/openid-configuration")
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10.0)
            resp.raise_for_status()
            self._oidc_config = resp.json()
        return self._oidc_config

    async def _get_jwk_client(self) -> PyJWKClient:
        if self._jwk_client is not None:
            return self._jwk_client
        config = await self.get_well_known_config()
        jwks_uri = config.get("jwks_uri")
        if not jwks_uri:
            raise RuntimeError("Could not find jwks_uri in OIDC discovery")
        # PyJWKClient handles fetching, caching, and key rotation.
        self._jwk_client = PyJWKClient(jwks_uri, cache_keys=True, lifespan=300)
        return self._jwk_client

    async def verify_token(self, token: str) -> dict[str, Any] | None:
        """Verify a Bearer JWT against the configured issuer's JWKS.

        Returns the decoded claims on success, None on any failure.
        Validates signature, issuer, expiry. Audience is checked permissively
        (accepts client_id or server_url) because providers vary in what they
        put in `aud` for tokens minted via PKCE.
        """
        try:
            jwk_client = await self._get_jwk_client()
            signing_key = jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
                options={"verify_aud": False, "require": ["exp", "iss"]},
            )
        except jwt.InvalidTokenError as exc:
            logger.warning("Bearer token rejected: %s", exc)
            return None
        except Exception as exc:  # JWKS fetch / network errors
            logger.error("Token verification failed: %s", exc)
            return None

        # Issuer match, tolerant of trailing slash on either side. Some
        # providers (Authentik for app-specific issuers) omit the slash; users
        # may include it in OAUTH_ISSUER_URL — accept both.
        token_iss = (claims.get("iss") or "").rstrip("/")
        if token_iss != self.issuer_url:
            logger.warning("Bearer token iss=%s did not match %s", token_iss, self.issuer_url)
            return None

        # Permissive audience check: accept if aud matches client_id, server_url,
        # or is absent. Authentik tokens for PKCE clients may not include aud.
        aud = claims.get("aud")
        if aud is not None:
            aud_list = aud if isinstance(aud, list) else [aud]
            if not any(a in (self.client_id, self.server_url) for a in aud_list):
                logger.warning(
                    "Bearer token aud=%s did not match client_id or server_url", aud
                )
                return None

        # Belt-and-braces expiry check (PyJWT already does this, but be explicit).
        exp = claims.get("exp")
        if exp is not None and time.time() >= exp:
            return None

        return claims


def extract_bearer_token(headers: dict[bytes, bytes] | list[tuple[bytes, bytes]]) -> str | None:
    """Pull the Bearer token out of an Authorization header.

    Accepts either a dict-like or the ASGI list-of-tuples form.
    """
    if isinstance(headers, list):
        auth = next((v for k, v in headers if k.lower() == b"authorization"), None)
    else:
        auth = headers.get(b"authorization") or headers.get(b"Authorization")
    if not auth:
        return None
    value = auth.decode("latin-1") if isinstance(auth, bytes) else auth
    if not value.lower().startswith("bearer "):
        return None
    token = value[7:].strip()
    return token or None
