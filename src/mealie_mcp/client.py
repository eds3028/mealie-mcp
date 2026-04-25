"""Async HTTP client for the Mealie REST API."""

from __future__ import annotations

from typing import Any

import httpx


class MealieError(RuntimeError):
    """Raised when the Mealie API returns an error response."""

    def __init__(self, status_code: int, message: str, payload: Any = None) -> None:
        super().__init__(f"Mealie API error {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.payload = payload


class MealieClient:
    """Thin async wrapper around the Mealie HTTP API.

    Only the endpoints required by the MCP tools are exposed. Each method
    returns the parsed JSON body and raises ``MealieError`` on non-2xx
    responses.
    """

    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> MealieClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        clean_params: dict[str, Any] | None = None
        if params is not None:
            clean_params = {k: v for k, v in params.items() if v is not None}

        response = await self._client.request(method, path, params=clean_params, json=json)
        if response.status_code >= 400:
            try:
                payload = response.json()
                detail = payload.get("detail") if isinstance(payload, dict) else payload
            except ValueError:
                payload = response.text
                detail = response.text
            raise MealieError(response.status_code, str(detail), payload)

        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # ---- Recipes -----------------------------------------------------------------

    async def search_recipes(
        self,
        *,
        query: str | None = None,
        tags: list[str] | None = None,
        per_page: int = 25,
        page: int = 1,
    ) -> dict[str, Any]:
        """Search recipes. ``tags`` are matched by slug."""
        params: dict[str, Any] = {
            "search": query,
            "perPage": per_page,
            "page": page,
        }
        if tags:
            params["tags"] = tags
        return await self._request("GET", "/api/recipes", params=params)

    async def get_recipe(self, slug: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/recipes/{slug}")

    async def create_recipe(self, name: str) -> str:
        """Create an empty recipe with the given name. Returns the new slug."""
        result = await self._request("POST", "/api/recipes", json={"name": name})
        if isinstance(result, str):
            return result
        if isinstance(result, dict) and "slug" in result:
            return result["slug"]
        raise MealieError(500, "Unexpected response from create_recipe", result)

    async def update_recipe(self, slug: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Partially update a recipe. Mealie requires the full resource on PUT,
        so we fetch, merge, and send back.
        """
        current = await self.get_recipe(slug)
        if not isinstance(current, dict):
            raise MealieError(500, "Unexpected response from get_recipe", current)
        merged = {**current, **patch}
        return await self._request("PUT", f"/api/recipes/{slug}", json=merged)

    async def import_recipe_from_url(self, url: str, *, include_tags: bool = True) -> dict[str, Any]:
        """Scrape a recipe from an external URL and save it to Mealie."""
        result = await self._request(
            "POST", "/api/recipes/create/url", json={"url": url, "includeTags": include_tags}
        )
        # Mealie returns the slug string; fetch the full recipe for a useful response.
        if isinstance(result, str):
            return await self.get_recipe(result)
        return result

    async def delete_recipe(self, slug: str) -> None:
        await self._request("DELETE", f"/api/recipes/{slug}")

    # ---- Meal plans --------------------------------------------------------------

    async def get_todays_meal_plan(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/api/households/mealplans/today")
        if isinstance(result, list):
            return result
        return []

    async def list_meal_plan(self, start_date: str, end_date: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/api/households/mealplans",
            params={"start_date": start_date, "end_date": end_date, "perPage": 1000},
        )

    async def create_meal_plan_entry(
        self,
        *,
        date: str,
        entry_type: str,
        recipe_id: str | None = None,
        title: str | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"date": date, "entryType": entry_type}
        if recipe_id is not None:
            body["recipeId"] = recipe_id
        if title is not None:
            body["title"] = title
        if text is not None:
            body["text"] = text
        return await self._request("POST", "/api/households/mealplans", json=body)

    async def delete_meal_plan_entry(self, entry_id: str) -> None:
        await self._request("DELETE", f"/api/households/mealplans/{entry_id}")

    # ---- Organizers: Tags --------------------------------------------------------

    async def list_tags(self, *, per_page: int = 1000) -> dict[str, Any]:
        return await self._request("GET", "/api/organizers/tags", params={"perPage": per_page})

    async def create_tag(self, name: str) -> dict[str, Any]:
        return await self._request("POST", "/api/organizers/tags", json={"name": name})

    async def get_or_create_tag(self, name: str) -> dict[str, Any]:
        """Return existing tag by name (case-insensitive) or create it."""
        result = await self.list_tags()
        items = result.get("items") if isinstance(result, dict) else result
        for tag in (items or []):
            if isinstance(tag, dict) and tag.get("name", "").lower() == name.lower():
                return tag
        return await self.create_tag(name)

    # ---- Organizers: Categories --------------------------------------------------

    async def list_categories(self, *, per_page: int = 1000) -> dict[str, Any]:
        return await self._request("GET", "/api/organizers/categories", params={"perPage": per_page})

    async def create_category(self, name: str) -> dict[str, Any]:
        return await self._request("POST", "/api/organizers/categories", json={"name": name})

    async def get_or_create_category(self, name: str) -> dict[str, Any]:
        """Return existing category by name (case-insensitive) or create it."""
        result = await self.list_categories()
        items = result.get("items") if isinstance(result, dict) else result
        for cat in (items or []):
            if isinstance(cat, dict) and cat.get("name", "").lower() == name.lower():
                return cat
        return await self.create_category(name)

    # ---- Organizers: Tools (equipment) ------------------------------------------

    async def list_recipe_tools(self, *, per_page: int = 1000) -> dict[str, Any]:
        return await self._request("GET", "/api/organizers/tools", params={"perPage": per_page})

    async def create_recipe_tool(self, name: str) -> dict[str, Any]:
        return await self._request("POST", "/api/organizers/tools", json={"name": name})

    async def get_or_create_recipe_tool(self, name: str) -> dict[str, Any]:
        """Return existing tool by name (case-insensitive) or create it."""
        result = await self.list_recipe_tools()
        items = result.get("items") if isinstance(result, dict) else result
        for tool in (items or []):
            if isinstance(tool, dict) and tool.get("name", "").lower() == name.lower():
                return tool
        return await self.create_recipe_tool(name)

    # ---- Images ------------------------------------------------------------------

    _EXT_MAP = {"image/png": "png", "image/gif": "gif", "image/webp": "webp"}

    async def _upload_recipe_image(self, slug: str, content: bytes, content_type: str) -> Any:
        ext = self._EXT_MAP.get(content_type, "jpg")
        response = await self._client.put(
            f"/api/recipes/{slug}/image",
            files={"image": (f"image.{ext}", content, content_type)},
            data={"extension": ext},
        )
        if response.status_code >= 400:
            try:
                payload = response.json()
                detail = payload.get("detail") if isinstance(payload, dict) else payload
            except ValueError:
                payload = response.text
                detail = response.text
            raise MealieError(response.status_code, str(detail), payload)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def upload_recipe_image_from_url(self, slug: str, url: str) -> Any:
        """Download an image from *url* and upload it to the recipe."""
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as dl:
            resp = await dl.get(url)
            if resp.status_code >= 400:
                raise MealieError(resp.status_code, f"Failed to download image from {url}")
            content = resp.content
            content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        return await self._upload_recipe_image(slug, content, content_type)

    async def upload_recipe_image_from_base64(
        self, slug: str, b64_data: str, content_type: str
    ) -> Any:
        """Decode a base64 image string and upload it to the recipe."""
        import base64
        try:
            content = base64.b64decode(b64_data)
        except Exception as exc:
            raise MealieError(400, f"Invalid base64 data: {exc}") from exc
        return await self._upload_recipe_image(slug, content, content_type)

    # ---- Shopping lists ----------------------------------------------------------

    async def create_shopping_list(self, name: str) -> dict[str, Any]:
        return await self._request("POST", "/api/households/shopping/lists", json={"name": name})

    async def list_shopping_lists(self) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/api/households/shopping/lists",
            params={"perPage": 1000},
        )

    async def list_shopping_list_items(self, list_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/api/households/shopping/items",
            params={"shoppingListId": list_id, "perPage": 1000},
        )

    async def add_shopping_list_item(
        self,
        *,
        list_id: str,
        note: str,
    ) -> dict[str, Any]:
        body = {"shoppingListId": list_id, "note": note, "isFood": False, "checked": False}
        return await self._request("POST", "/api/households/shopping/items", json=body)

    async def check_off_shopping_item(self, item_id: str, *, checked: bool = True) -> dict[str, Any]:
        """Toggle the checked state of a shopping list item."""
        current = await self._request("GET", f"/api/households/shopping/items/{item_id}")
        if not isinstance(current, dict):
            raise MealieError(500, "Unexpected response fetching shopping item", current)
        return await self._request(
            "PUT",
            f"/api/households/shopping/items/{item_id}",
            json={**current, "checked": checked},
        )

    async def delete_shopping_list_item(self, item_id: str) -> None:
        await self._request("DELETE", f"/api/households/shopping/items/{item_id}")

    # ---- Foods -------------------------------------------------------------------

    async def list_foods(self, *, query: str | None = None, per_page: int = 50) -> dict[str, Any]:
        params: dict[str, Any] = {"perPage": per_page}
        if query:
            params["search"] = query
        return await self._request("GET", "/api/foods", params=params)

    # ---- Cookbooks ---------------------------------------------------------------

    async def list_cookbooks(self) -> dict[str, Any]:
        return await self._request("GET", "/api/households/cookbooks", params={"perPage": 1000})

    async def create_cookbook(
        self, name: str, *, description: str = "", public: bool = False
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/households/cookbooks",
            json={"name": name, "description": description, "public": public},
        )
