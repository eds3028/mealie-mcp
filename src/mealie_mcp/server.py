"""MCP server exposing Mealie as a set of LLM-callable tools."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP

from .client import MealieClient, MealieError

EntryType = Literal["breakfast", "lunch", "dinner", "side"]


@dataclass
class AppContext:
    client: MealieClient


def _load_settings() -> tuple[str, str]:
    base_url = os.environ.get("MEALIE_URL", "").strip()
    token = os.environ.get("MEALIE_API_TOKEN", "").strip()
    if not base_url:
        raise RuntimeError("MEALIE_URL environment variable is required")
    if not token:
        raise RuntimeError("MEALIE_API_TOKEN environment variable is required")
    return base_url, token


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
    base_url, token = _load_settings()
    client = MealieClient(base_url=base_url, api_token=token)
    try:
        yield AppContext(client=client)
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


def _client(ctx: Context) -> MealieClient:
    return ctx.request_context.lifespan_context.client


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
    async def create_recipe(ctx: Context, name: str) -> dict[str, str]:
        """Create a blank recipe with the given name. Returns the generated slug."""
        try:
            slug = await _client(ctx).create_recipe(name)
        except MealieError as exc:
            raise RuntimeError(str(exc)) from exc
        return {"slug": slug, "name": name}

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
    ) -> dict[str, Any]:
        """Update fields on an existing recipe. Only provided fields are changed.

        Use this after ``create_recipe`` to populate ingredients, instructions,
        description, servings, and other content. ``ingredients``, ``instructions``,
        and ``notes`` replace the existing lists when provided.

        Args:
            slug: The recipe slug returned by ``create_recipe`` or ``search_recipes``.
            name: New recipe name.
            description: Recipe description / summary.
            recipe_yield: Free-text yield, e.g. "8 servings" or "1 loaf".
            recipe_servings: Numeric serving count, e.g. 4.
            prep_time: Free-text prep time, e.g. "15 min".
            cook_time: Free-text cook time, e.g. "30 min".
            total_time: Free-text total time.
            ingredients: Ingredient lines as plain strings, e.g. ["2 cups flour", "1 tsp salt"].
            instructions: Ordered instruction steps as plain strings.
            notes: Free-text recipe notes (one entry per note).
        """
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
            patch["recipeIngredient"] = [{"note": line} for line in ingredients]
        if instructions is not None:
            patch["recipeInstructions"] = [{"text": step} for step in instructions]
        if notes is not None:
            patch["notes"] = [{"title": "", "text": text} for text in notes]

        if not patch:
            raise ValueError("Provide at least one field to update")

        try:
            updated = await _client(ctx).update_recipe(slug, patch)
        except MealieError as exc:
            raise RuntimeError(str(exc)) from exc
        return _summarize_recipe(updated) if isinstance(updated, dict) else {"slug": slug}

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
