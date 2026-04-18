import os
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.getenv("MONGODB_DB", "menu_generator")

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(MONGODB_URI)
    return _client


def close_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


def _collection():
    return get_client()[MONGODB_DB]["menus"]


async def list_menus() -> list[dict]:
    col = _collection()
    cursor = col.find({}, {
        "_id": 1, "name": 1, "source_file": 1, "file_type": 1,
        "side": 1, "page": 1, "num_elements": 1, "num_categories": 1, "created_at": 1,
    })
    results = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        results.append(doc)
    return results


async def get_menu_data(menu_id: str) -> dict | None:
    from bson import ObjectId
    col = _collection()
    doc = await col.find_one({"_id": ObjectId(menu_id)}, {"menu_data": 1})
    if doc is None:
        return None
    return doc.get("menu_data")


async def get_template(menu_id: str) -> dict | None:
    from bson import ObjectId
    col = _collection()
    doc = await col.find_one({"_id": ObjectId(menu_id)}, {"template": 1})
    if doc is None:
        return None
    return doc.get("template")


async def upsert_menu(
    name: str,
    source_file: str,
    file_type: str,
    side: str,
    page: int,
    menu_data: dict,
    template: dict,
) -> str:
    col = _collection()
    num_elements = len(template.get("elements", []))
    num_categories = len(menu_data.get("categories", []))

    doc: dict[str, Any] = {
        "name": name,
        "source_file": source_file,
        "file_type": file_type,
        "side": side,
        "page": page,
        "menu_data": menu_data,
        "template": template,
        "num_elements": num_elements,
        "num_categories": num_categories,
        "updated_at": datetime.now(timezone.utc),
    }

    result = await col.update_one(
        {"name": name, "side": side, "page": page},
        {"$set": doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )

    if result.upserted_id:
        return str(result.upserted_id)
    existing = await col.find_one({"name": name, "side": side, "page": page}, {"_id": 1})
    return str(existing["_id"])
