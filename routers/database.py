"""
routers/database.py
───────────────────
ナレッジカード CRUD エンドポイント。
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from butly_core.core.database import ButlyDatabase
import dependencies as deps

router = APIRouter()


class UpdateCardRequest(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[str] = None
    summary: Optional[str] = None
    episode: Optional[str] = None
    ai_importance: Optional[int] = None
    humanity_importance: Optional[int] = None


class CardPinRequest(BaseModel):
    is_pinned: bool


@router.get("/database/cards/{instance_name}")
def get_database_cards(
    instance_name: str,
    limit: int = 50,
    offset: int = 0,
    category: Optional[str] = None,
    search: Optional[str] = None,
):
    """Get knowledge cards from the instance's database."""
    db_path = deps.INSTANCES_DIR / instance_name / "butly_memory.db"
    db = ButlyDatabase(db_path=str(db_path))
    cards = db.get_cards(limit=limit, offset=offset, category=category, search=search)
    return cards


@router.get("/database/cards/{instance_name}/{card_id}")
def get_database_card(instance_name: str, card_id: str):
    """Get details of a specific knowledge card."""
    db_path = deps.INSTANCES_DIR / instance_name / "butly_memory.db"
    db = ButlyDatabase(db_path=str(db_path))
    card = db.get_card(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    return card


@router.put("/database/cards/{instance_name}/{card_id}")
def update_database_card(instance_name: str, card_id: str, request: UpdateCardRequest):
    """Update a knowledge card."""
    db_path = deps.INSTANCES_DIR / instance_name / "butly_memory.db"
    db = ButlyDatabase(db_path=str(db_path))
    update_data = request.dict(exclude_none=True)
    if not update_data:
        return {"message": "No fields to update"}
    success = db.update_card(card_id, update_data)
    if not success:
        raise HTTPException(
            status_code=404, detail="Card not found or failed to update"
        )
    return {"message": "Card updated successfully"}


@router.delete("/database/cards/{instance_name}/{card_id}")
def delete_database_card(instance_name: str, card_id: str):
    """Delete a knowledge card."""
    db_path = deps.INSTANCES_DIR / instance_name / "butly_memory.db"
    db = ButlyDatabase(db_path=str(db_path))
    success = db.delete_card(card_id)
    if not success:
        raise HTTPException(
            status_code=404, detail="Card not found or failed to delete"
        )
    return {"message": "Card deleted successfully"}


@router.post("/database/cards/{instance_name}/{card_id}/pin")
def pin_database_card(instance_name: str, card_id: str, request: CardPinRequest):
    """Pin or unpin a knowledge card, and if pinning, append it to Key_Memory.txt."""
    db_path = deps.INSTANCES_DIR / instance_name / "butly_memory.db"
    db = ButlyDatabase(db_path=str(db_path))

    success = db.toggle_pin(card_id, request.is_pinned)
    if not success:
        raise HTTPException(
            status_code=404, detail="Card not found or failed to update pin state"
        )

    if request.is_pinned:
        card = db.get_card(card_id)
        if card:
            km_path = deps.INSTANCES_DIR / instance_name / "Key_Memory.txt"
            additional_text = f"\n\n[Pinned Memory: {card['title']}]\n{card['summary']}"
            if km_path.exists():
                with open(km_path, "a", encoding="utf-8") as f:
                    f.write(additional_text)
            else:
                km_path.write_text(additional_text.strip(), encoding="utf-8")

    return {
        "message": "Card pin state updated successfully",
        "is_pinned": request.is_pinned,
    }
