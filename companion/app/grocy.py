"""Thin async client for the Grocy REST API (header GROCY-API-KEY)."""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import httpx


class GrocyError(RuntimeError):
    def __init__(self, status: int, message: str, path: str) -> None:
        super().__init__(f"Grocy {status} on {path}: {message}")
        self.status = status
        self.message = message
        self.path = path


class GrocyClient:
    def __init__(self, api_url: str, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self.api_url = api_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=60.0)
        self._headers = {"GROCY-API-KEY": api_key, "Accept": "application/json"}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {**self._headers, **kwargs.pop("headers", {})}
        resp = await self._client.request(method, self.api_url + path, headers=headers, **kwargs)
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("error_message", resp.text)
            except ValueError:
                msg = resp.text
            raise GrocyError(resp.status_code, str(msg)[:500], path)
        if resp.status_code == 204 or not resp.content:
            return None
        ctype = resp.headers.get("content-type", "")
        return resp.json() if "json" in ctype else resp.content

    async def get(self, path: str, params: Any = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: Any = None) -> Any:
        return await self._request("POST", path, json=body if body is not None else {})

    async def put(self, path: str, body: Any) -> Any:
        return await self._request("PUT", path, json=body)

    async def delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    # --- generic objects -------------------------------------------------
    async def objects(self, entity: str, query: list[str] | None = None) -> list[dict[str, Any]]:
        params = [("query[]", q) for q in (query or [])]
        return await self.get(f"/objects/{entity}", params=params or None) or []

    async def object(self, entity: str, object_id: int) -> dict[str, Any]:
        return await self.get(f"/objects/{entity}/{object_id}")

    async def add_object(self, entity: str, data: dict[str, Any]) -> int:
        res = await self.post(f"/objects/{entity}", data)
        return int(res["created_object_id"])

    async def edit_object(self, entity: str, object_id: int, data: dict[str, Any]) -> None:
        await self.put(f"/objects/{entity}/{object_id}", data)

    async def delete_object(self, entity: str, object_id: int) -> None:
        await self.delete(f"/objects/{entity}/{object_id}")

    async def userfields(self, entity: str, object_id: int) -> dict[str, Any]:
        return await self.get(f"/userfields/{entity}/{object_id}") or {}

    async def set_userfields(self, entity: str, object_id: int, values: dict[str, Any]) -> None:
        await self.put(f"/userfields/{entity}/{object_id}", values)

    # --- stock -----------------------------------------------------------
    async def stock(self) -> list[dict[str, Any]]:
        return await self.get("/stock") or []

    async def volatile(self, due_soon_days: int | None = None) -> dict[str, Any]:
        params = {"due_soon_days": due_soon_days} if due_soon_days is not None else None
        return await self.get("/stock/volatile", params=params)

    async def product_details(self, product_id: int) -> dict[str, Any]:
        return await self.get(f"/stock/products/{product_id}")

    async def product_by_barcode(self, barcode: str) -> dict[str, Any] | None:
        try:
            return await self.get(f"/stock/products/by-barcode/{quote(barcode, safe='')}")
        except GrocyError as e:
            if e.status in (400, 404):
                return None
            raise

    async def stock_entries(self, product_id: int) -> list[dict[str, Any]]:
        return await self.get(f"/stock/products/{product_id}/entries") or []

    async def add_stock(self, product_id: int, amount: float, **extra: Any) -> Any:
        body = {"amount": amount, **{k: v for k, v in extra.items() if v is not None}}
        return await self.post(f"/stock/products/{product_id}/add", body)

    async def consume_stock(self, product_id: int, amount: float, **extra: Any) -> Any:
        body = {"amount": amount, **{k: v for k, v in extra.items() if v is not None}}
        return await self.post(f"/stock/products/{product_id}/consume", body)

    async def open_stock(self, product_id: int, amount: float, stock_entry_id: str | None = None) -> Any:
        body: dict[str, Any] = {"amount": amount}
        if stock_entry_id:
            body["stock_entry_id"] = stock_entry_id
        return await self.post(f"/stock/products/{product_id}/open", body)

    async def edit_stock_entry(self, entry_id: int, body: dict[str, Any]) -> Any:
        return await self.put(f"/stock/entry/{entry_id}", body)

    async def inventory(self, product_id: int, new_amount: float, **extra: Any) -> Any:
        body = {"new_amount": new_amount, **{k: v for k, v in extra.items() if v is not None}}
        return await self.post(f"/stock/products/{product_id}/inventory", body)

    async def undo_transaction(self, transaction_id: str) -> Any:
        return await self.post(f"/stock/transactions/{transaction_id}/undo")

    # --- recipes / shopping list ----------------------------------------
    async def recipe_fulfillment(self, recipe_id: int) -> dict[str, Any]:
        return await self.get(f"/recipes/{recipe_id}/fulfillment")

    async def consume_recipe(self, recipe_id: int) -> Any:
        return await self.post(f"/recipes/{recipe_id}/consume")

    async def add_missing_to_shoppinglist(self, recipe_id: int) -> Any:
        return await self.post(f"/recipes/{recipe_id}/add-not-fulfilled-products-to-shoppinglist")

    async def shoppinglist_add(self, product_id: int, amount: float, note: str | None = None) -> Any:
        body: dict[str, Any] = {"product_id": product_id, "product_amount": amount}
        if note:
            body["note"] = note
        return await self.post("/stock/shoppinglist/add-product", body)

    # --- files -----------------------------------------------------------
    async def upload_file(self, group: str, file_name: str, data: bytes) -> None:
        encoded = base64.b64encode(file_name.encode()).decode()
        await self._request(
            "PUT",
            f"/files/{group}/{encoded}",
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )


def file_userfield_value(stored_name: str, display_name: str) -> str:
    """Value format Grocy's UI uses for file/image userfields: b64(stored)_b64(display)."""
    b = base64.b64encode
    return f"{b(stored_name.encode()).decode()}_{b(display_name.encode()).decode()}"
