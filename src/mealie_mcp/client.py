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

    # ---- Meal plans --------------------------------------------------------------

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

    # ---- Shopping lists ----------------------------------------------------------

    async def list_shopping_lists(self) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/api/households/shopping/lists",
            params={"perPage": 1000},
        )

    async def add_shopping_list_item(
        self,
        *,
        list_id: str,
        note: str,
    ) -> dict[str, Any]:
        body = {"shoppingListId": list_id, "note": note, "isFood": False, "checked": False}
        return await self._request("POST", "/api/households/shopping/items", json=body)
