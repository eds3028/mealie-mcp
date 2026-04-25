"""MCP server exposing Mealie as a set of LLM-callable tools."""

from __future__ import annotations

import json
import os
import secrets
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urljoin

from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from .client import MealieClient, MealieError
from .auth import OAuthConfig, verify_oauth_token

logger = logging.getLogger(__name__)

EntryType = Literal["breakfast", "lunch", "dinner", "side"]


@dataclass
class AppContext:
    client: MealieClient
    oauth_config: OAuthConfig | None = None
    oauth_sessions: dict[str, str] = None  # state -> access_token mapping

    def __post_init__(self):
        if self.oauth_sessions is None:
            self.oauth_sessions = {}


def _load_settings() -> tuple[str, str, OAuthConfig | None]:
    base_url = os.environ.get("MEALIE_URL", "").strip()
    token = os.environ.get("MEALIE_API_TOKEN", "").strip()
    if not base_url:
        raise RuntimeError("MEALIE_URL environment variable is required")
    if not token:
        raise RuntimeError("MEALIE_API_TOKEN environment variable is required")

    oauth_config = None
    oauth_issuer = os.environ.get("OAUTH_ISSUER_URL", "").strip()
    oauth_client_id = os.environ.get("OAUTH_CLIENT_ID", "").strip()
    oauth_client_secret = os.environ.get("OAUTH_CLIENT_SECRET", "").strip()
    oauth_server_url = os.environ.get("OAUTH_SERVER_URL", "").strip()

    if oauth_issuer and oauth_client_id and oauth_client_secret and oauth_server_url:
        oauth_config = OAuthConfig(
            issuer_url=oauth_issuer,
            client_id=oauth_client_id,
            client_secret=oauth_client_secret,
            server_url=oauth_server_url,
        )

    return base_url, token, oauth_config


def _configure_transport_security(server: FastMCP) -> None:
    """Configure DNS rebinding protection and CORS based on env vars."""
    allowed_hosts = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
    allowed_origins = os.environ.get("MCP_ALLOWED_ORIGINS", "").strip()

    if allowed_hosts:
        hosts = [h.strip() for h in allowed_hosts.split(",") if h.strip()]
        server.settings.transport_security.allowed_hosts.extend(hosts)

    if allowed_origins:
        origins = [o.strip() for o in allowed_origins.split(",") if o.strip()]
        server.settings.transport_security.allowed_origins.extend(origins)


@asynccontextmanager
async def _lifespan(_server: FastMCP):
    base_url, token, oauth_config = _load_settings()
    client = MealieClient(base_url=base_url, api_token=token)
    try:
        yield AppContext(client=client, oauth_config=oauth_config)
    finally:
        await client.aclose()


def _summarize_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
    """Trim a Mealie recipe payload to the fields useful for search results."""
    return {
        "slug": recipe.get("slug"),
        "name": recipe.get("name"),
        "description": recipe.get("description"),
        "tags": [t.get("name") for t in recipe.get("tags") or [] if isinstance(t, dict)],
        "categories": [
            c.get("name") for c in recipe.get("recipeCategory") or [] if isinstance(c, dict)
        ],
    }


def _app_context(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context


def _client(ctx: Context) -> MealieClient:
    return _app_context(ctx).client


def _require_oauth(ctx: Context) -> None:
    """Raise an error if OAuth is required but token is missing/invalid."""
    app = _app_context(ctx)
    if app.oauth_config is not None:
        token_info = verify_oauth_token(ctx)
        if token_info is None:
            raise RuntimeError(
                "Authorization required. This MCP server requires OAuth authentication. "
                "Please authorize via your MCP client's auth flow."
            )


def _section_title(line: str) -> str | None:
    stripped = line.lstrip()
    for prefix in ("### ", "## ", "# "):
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return None


def _ingredient_from_line(line: str) -> dict[str, Any]:
    title = _section_title(line)
    if title is not None:
        return {"title": title, "note": "", "disableAmount": True}
    return {"note": line}


def _instruction_from_line(line: str) -> dict[str, Any]:
    title = _section_title(line)
    if title is not None:
        return {"title": title, "text": ""}
    return {"text": line}


def _build_recipe_patch(
    *,
    name: str | None = None,
    description: str | None = None,
    recipe_yield: str | None = None,
    recipe_servings: float | None = None,
    prep_time: str | None = None,
    cook_time: str | None = None,
    total_time: str | None = None,
    ingredients: list[str] | None = None,
    instructions: list[str] | None = None,
    notes: list[str] | None = None,
    tag_objects: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if name is not None:
        patch["name"] = name
    if description is not None:
        patch["description"] = description
    if recipe_yield is not None:
        patch["recipeYield"] = recipe_yield
    if recipe_servings is not None:
        patch["recipeServings"] = recipe_servings
    if prep_time is not None:
        patch["prepTime"] = prep_time
    if cook_time is not None:
        patch["cookTime"] = cook_time
    if total_time is not None:
        patch["totalTime"] = total_time
    if ingredients is not None:
        patch["recipeIngredient"] = [_ingredient_from_line(line) for line in ingredients]
    if instructions is not None:
        patch["recipeInstructions"] = [_instruction_from_line(line) for line in instructions]
    if notes is not None:
        patch["notes"] = [{"title": "", "text": text} for text in notes]
    if tag_objects is not None:
        patch["tags"] = tag_objects
    return patch


def build_server() -> FastMCP:
    """Construct the FastMCP server with all Mealie tools registered."""
    mcp = FastMCP(
        "mealie-mcp",
        instructions=(
            "Tools to query and manage a self-hosted Mealie recipe instance: "
            "search recipes, fetch details, manage meal plans, and edit shopping lists."
        ),
        lifespan=_lifespan,
    )

    # Load OAuth config once at build time for HTTP route handlers
    # (custom_route handlers don't have access to the lifespan context)
    _, _, oauth_config = _load_settings()
    oauth_sessions: dict[str, str] = {}

    # OAuth well-known endpoints (MCP spec)
    @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    async def oauth_protected_resource(request: Request) -> JSONResponse:
        """Advertise that this is an OAuth-protected resource."""
        if oauth_config is None:
            return JSONResponse({"error": "OAuth not configured"}, status_code=404)

        return JSONResponse(
            {
                "resource": oauth_config.server_url,
                "authorization_servers": [oauth_config.issuer_url],
            }
        )

    @mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def oauth_authorization_server(request: Request) -> JSONResponse:
        """Return OIDC discovery document."""
        if oauth_config is None:
            return JSONResponse({"error": "OAuth not configured"}, status_code=404)

        try:
            config = await oauth_config.get_well_known_config()
            return JSONResponse(config)
        except Exception as e:
            logger.error(f"Failed to fetch OIDC config: {e}")
            return JSONResponse({"error": "Failed to fetch OAuth config"}, status_code=500)

    @mcp.custom_route("/oauth/authorize", methods=["GET"])
    async def oauth_authorize(request: Request) -> RedirectResponse:
        """Initiate OAuth authorization flow."""
        if oauth_config is None:
            return JSONResponse({"error": "OAuth not configured"}, status_code=404)

        state = secrets.token_urlsafe(32)
        auth_url = oauth_config.get_authorization_url(state)
        return RedirectResponse(url=auth_url)

    @mcp.custom_route("/oauth/callback", methods=["GET"])
    async def oauth_callback(request: Request) -> JSONResponse:
        """Handle OAuth callback from authorization server."""
        if oauth_config is None:
            return JSONResponse({"error": "OAuth not configured"}, status_code=404)

        code = request.query_params.get("code")
        state = request.query_params.get("state")
        error = request.query_params.get("error")

        if error:
            return JSONResponse(
                {"error": f"Authorization failed: {error}"},
                status_code=400,
            )

        if not code or not state:
            return JSONResponse({"error": "Missing code or state"}, status_code=400)

        try:
            token_response = await oauth_config.exchange_code_for_token(code)
            access_token = token_response.get("access_token")
            if not access_token:
                raise ValueError("No access_token in response")

            oauth_sessions[state] = access_token
            return JSONResponse(
                {
                    "status": "success",
                    "message": "Authorization successful. Return this to your MCP client.",
                    "access_token": access_token,
                }
            )
        except Exception as e:
            logger.error(f"Token exchange failed: {e}")
            return JSONResponse(
                {"error": f"Token exchange failed: {e}"},
                status_code=500,
            )

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        """Simple health check endpoint."""
        return JSONResponse({"status": "ok", "oauth_enabled": oauth_config is not None})

    @mcp.tool()
    async def search_recipes(
        ctx: Context,
        query: str | None = None,
        tags: list[str] | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Search recipes in Mealie.

        Args:
            query: Free-text search across recipe names and descriptions.
            tags: Optional list of tag slugs to filter by.
            limit: Maximum number of recipes to return (default 25, max 100).
        """
        per_page = max(1, min(limit, 100))
        try:
            payload = await _client(ctx).search_recipes(
                query=query, tags=tags, per_page=per_page
            )
        except MealieError as exc:
            raise RuntimeError(str(exc)) from exc
        items = payload.get("items") if isinstance(payload, dict) else payload
        return [_summarize_recipe(r) for r in (items or [])]

    @mcp.tool()
    async def get_recipe(ctx: Context, slug: str) -> dict[str, Any]:
        """Fetch the full recipe JSON for a given slug."""
        try:
            return await _client(ctx).get_recipe(slug)
        except MealieError as exc:
            raise RuntimeError(str(exc)) from exc

    @mcp.tool()
    async def list_meal_plan(
        ctx: Context, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """List meal plan entries between two dates (inclusive).

        Args:
            start_date: ISO date string, e.g. "2026-04-24".
            end_date: ISO date string, e.g. "2026-05-01".
        """
        try:
            payload = await _client(ctx).list_meal_plan(start_date, end_date)
        except MealieError as exc:
            raise RuntimeError(str(exc)) from exc
        items = payload.get("items") if isinstance(payload, dict) else payload
        return items or []

    @mcp.tool()
    async def list_shopping_lists(ctx: Context) -> list[dict[str, Any]]:
        """Return the IDs and names of all shopping lists."""
        try:
            payload = await _client(ctx).list_shopping_lists()
        except MealieError as exc:
            raise RuntimeError(str(exc)) from exc
        items = payload.get("items") if isinstance(payload, dict) else payload
        return [
            {"id": item.get("id"), "name": item.get("name")}
            for item in (items or [])
            if isinstance(item, dict)
        ]

    @mcp.tool()
    async def add_shopping_list_items(
        ctx: Context, list_id: str, items: list[str]
    ) -> dict[str, Any]:
        """Add free-text items to a shopping list.

        Args:
            list_id: The shopping list ID (UUID) returned by list_shopping_lists.
            items: List of free-text item descriptions, e.g. ["2 lbs chicken thighs"].
        """
        added: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        client = _client(ctx)
        for note in items:
            text = note.strip()
            if not text:
                continue
            try:
                created = await client.add_shopping_list_item(list_id=list_id, note=text)
                added.append({"id": created.get("id") if isinstance(created, dict) else None, "note": text})
            except MealieError as exc:
                errors.append({"note": text, "error": str(exc)})
        return {"added": added, "errors": errors}

    @mcp.tool()
    async def create_recipe(
        ctx: Context,
        name: str,
        description: str | None = None,
        recipe_yield: str | None = None,
        recipe_servings: float | None = None,
        prep_time: str | None = None,
        cook_time: str | None = None,
        total_time: str | None = None,
        ingredients: list[str] | None = None,
        instructions: list[str] | None = None,
        notes: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a recipe fully populated with content in one call.

        Mealie's API requires a two-step flow (create shell, then update). This
        tool does both: it POSTs the shell, then PUTs the full body so the
        recipe is saved with all content.

        Ingredient and instruction lines that start with "# ", "## ", or "### "
        become section headers (e.g. "### Filling"). All other lines are plain
        ingredient text / instruction steps. ``notes`` are free-text notes.

        Args:
            name: Recipe name.
            description: Short summary shown above the recipe.
            recipe_yield: Free-text yield, e.g. "8 servings" or "1 loaf".
            recipe_servings: Numeric serving count, e.g. 4.
            prep_time: Free-text prep time, e.g. "15 min".
            cook_time: Free-text cook time, e.g. "30 min".
            total_time: Free-text total time.
            ingredients: Ingredient lines; "### Base" style lines become sections.
            instructions: Ordered steps; "### Base" style lines become sections.
            notes: Free-text recipe notes (one entry per note).
            tags: Tag names to apply. Tags are created in Mealie if they don't exist.
        """
        client = _client(ctx)
        try:
            slug = await client.create_recipe(name)
        except MealieError as exc:
            raise RuntimeError(str(exc)) from exc

        tag_objects: list[dict[str, Any]] | None = None
        if tags is not None:
            try:
                tag_objects = [await client.get_or_create_tag(t) for t in tags]
            except MealieError as exc:
                raise RuntimeError(f"Failed to resolve tags: {exc}") from exc

        patch = _build_recipe_patch(
            description=description,
            recipe_yield=recipe_yield,
            recipe_servings=recipe_servings,
            prep_time=prep_time,
            cook_time=cook_time,
            total_time=total_time,
            ingredients=ingredients,
            instructions=instructions,
            notes=notes,
            tag_objects=tag_objects,
        )
        if not patch:
            return {"slug": slug, "name": name}

        try:
            updated = await client.update_recipe(slug, patch)
        except MealieError as exc:
            raise RuntimeError(f"Recipe '{slug}' was created but update failed: {exc}") from exc
        return _summarize_recipe(updated) if isinstance(updated, dict) else {"slug": slug, "name": name}

    @mcp.tool()
    async def update_recipe(
        ctx: Context,
        slug: str,
        name: str | None = None,
        description: str | None = None,
        recipe_yield: str | None = None,
        recipe_servings: float | None = None,
        prep_time: str | None = None,
        cook_time: str | None = None,
        total_time: str | None = None,
        ingredients: list[str] | None = None,
        instructions: list[str] | None = None,
        notes: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update fields on an existing recipe. Only provided fields are changed.

        Section-header rules for ``ingredients`` and ``instructions`` match
        ``create_recipe``: lines starting with "# ", "## ", or "### " become
        section headers.

        Args:
            slug: Recipe slug returned by ``create_recipe`` or ``search_recipes``.
            name: New recipe name.
            description: Recipe description / summary.
            recipe_yield: Free-text yield, e.g. "8 servings" or "1 loaf".
            recipe_servings: Numeric serving count, e.g. 4.
            prep_time: Free-text prep time, e.g. "15 min".
            cook_time: Free-text cook time, e.g. "30 min".
            total_time: Free-text total time.
            ingredients: Ingredient lines; "### Base" style lines become sections.
            instructions: Ordered steps; "### Base" style lines become sections.
            notes: Free-text recipe notes (one entry per note).
            tags: Tag names to apply (replaces existing tags). Tags are created if they don't exist.
        """
        client = _client(ctx)

        tag_objects: list[dict[str, Any]] | None = None
        if tags is not None:
            try:
                tag_objects = [await client.get_or_create_tag(t) for t in tags]
            except MealieError as exc:
                raise RuntimeError(f"Failed to resolve tags: {exc}") from exc

        patch = _build_recipe_patch(
            name=name,
            description=description,
            recipe_yield=recipe_yield,
            recipe_servings=recipe_servings,
            prep_time=prep_time,
            cook_time=cook_time,
            total_time=total_time,
            ingredients=ingredients,
            instructions=instructions,
            notes=notes,
            tag_objects=tag_objects,
        )
        if not patch:
            raise ValueError("Provide at least one field to update")

        try:
            updated = await client.update_recipe(slug, patch)
        except MealieError as exc:
            raise RuntimeError(str(exc)) from exc
        return _summarize_recipe(updated) if isinstance(updated, dict) else {"slug": slug}

    @mcp.tool()
    async def list_tags(ctx: Context) -> list[dict[str, Any]]:
        """List all tags available in Mealie."""
        try:
            payload = await _client(ctx).list_tags()
        except MealieError as exc:
            raise RuntimeError(str(exc)) from exc
        items = payload.get("items") if isinstance(payload, dict) else payload
        return [
            {"id": t.get("id"), "name": t.get("name"), "slug": t.get("slug")}
            for t in (items or [])
            if isinstance(t, dict)
        ]

    @mcp.tool()
    async def set_recipe_tags(
        ctx: Context,
        slug: str,
        tags: list[str],
    ) -> dict[str, Any]:
        """Replace all tags on a recipe with the provided list.

        Tags that don't already exist in Mealie are created automatically.

        Args:
            slug: Recipe slug returned by ``search_recipes`` or ``create_recipe``.
            tags: Tag names to apply. Pass an empty list to clear all tags.
        """
        client = _client(ctx)
        try:
            tag_objects = [await client.get_or_create_tag(t) for t in tags]
            updated = await client.update_recipe(slug, {"tags": tag_objects})
        except MealieError as exc:
            raise RuntimeError(str(exc)) from exc
        return _summarize_recipe(updated) if isinstance(updated, dict) else {"slug": slug}

    @mcp.tool()
    async def set_recipe_image_from_url(
        ctx: Context,
        slug: str,
        url: str,
    ) -> dict[str, Any]:
        """Upload an image for a recipe by downloading it from a URL.

        The image replaces any previously set recipe image.

        Args:
            slug: Recipe slug returned by ``search_recipes`` or ``create_recipe``.
            url: Publicly accessible URL of the image (JPEG, PNG, GIF, or WebP).
        """
        try:
            await _client(ctx).upload_recipe_image_from_url(slug, url)
        except MealieError as exc:
            raise RuntimeError(str(exc)) from exc
        return {"slug": slug, "status": "image updated"}

    @mcp.tool()
    async def create_meal_plan_entry(
        ctx: Context,
        date: str,
        entry_type: EntryType,
        recipe_slug: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Add an entry to the meal plan.

        Either ``recipe_slug`` or ``title`` should be provided. ``recipe_slug``
        links to an existing recipe; ``title`` creates a free-text entry.

        Args:
            date: ISO date string for the meal, e.g. "2026-04-24".
            entry_type: One of "breakfast", "lunch", "dinner", "side".
            recipe_slug: Optional slug of an existing recipe to schedule.
            title: Optional free-text title (used when no recipe is linked).
        """
        if not recipe_slug and not title:
            raise ValueError("Provide either recipe_slug or title")

        client = _client(ctx)
        recipe_id: str | None = None
        if recipe_slug:
            try:
                recipe = await client.get_recipe(recipe_slug)
            except MealieError as exc:
                raise RuntimeError(f"Could not look up recipe '{recipe_slug}': {exc}") from exc
            recipe_id = recipe.get("id") if isinstance(recipe, dict) else None
            if not recipe_id:
                raise RuntimeError(f"Recipe '{recipe_slug}' has no id")

        try:
            return await client.create_meal_plan_entry(
                date=date,
                entry_type=entry_type,
                recipe_id=recipe_id,
                title=title,
            )
        except MealieError as exc:
            raise RuntimeError(str(exc)) from exc

    return mcp


def run() -> None:
    """Entry point: start the server using the configured transport."""
    transport = os.environ.get("MCP_TRANSPORT", "sse").strip().lower()

    server = build_server()

    if transport == "stdio":
        server.run(transport="stdio")
        return

    if transport in ("sse", "http", "streamable-http"):
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8000"))

        server.settings.host = host
        server.settings.port = port
        _configure_transport_security(server)

        if transport in ("sse", "http"):
            server.run(transport="sse")
        else:
            server.run(transport="streamable-http")
        return

    raise RuntimeError(
        f"Unknown MCP_TRANSPORT '{transport}'. Use 'stdio', 'sse', or 'streamable-http'."
    )


if __name__ == "__main__":
    run()
