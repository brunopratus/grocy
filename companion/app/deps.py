"""Shared service container passed to the feature modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .claude import ClaudeClient
from .config import Settings
from .db import Database
from .grocy import GrocyClient
from .setup import Catalog


@dataclass
class Services:
    settings: Settings
    grocy: GrocyClient
    claude: ClaudeClient
    db: Database
    catalog: Catalog
    http: httpx.AsyncClient  # outbound calls to Open Food Facts, USDA, ntfy

    async def products(self) -> list[dict[str, Any]]:
        return await self.grocy.objects("products")
