"""Picks service — create, update, delete, list asset 'likes'."""

from typing import List

from sqlmodel import Session, select
from models.community import CommunityPick, CommunityProfile
from models.user import User
from dtos.community import PickCreate, PickResponse, PickUpdate


def _pick_to_response(pick: CommunityPick, username: str) -> PickResponse:
    return PickResponse(
        id=pick.id,  # type: ignore[arg-type]
        username=username,
        symbol=pick.symbol,
        asset_type=pick.asset_type,
        comment=pick.comment,
        target_price=pick.target_price,
        created_at=pick.created_at.isoformat(),
        updated_at=pick.updated_at.isoformat(),
    )


def create_pick(session: Session, user_uuid: str, data: PickCreate) -> PickResponse:
    """Create a new pick (like) for an asset. Raises ValueError on duplicate."""
    user = session.get(User, user_uuid)
    if not user:
        raise ValueError("Utilisateur introuvable")

    # Ensure user has an active community profile
    profile = session.exec(
        select(CommunityProfile).where(
            CommunityProfile.user_id == user_uuid,
            CommunityProfile.is_active == True,
        )
    ).first()
    if not profile:
        raise ValueError("Activez votre profil communautaire dans les paramètres avant de liker un actif.")

    existing = session.exec(
        select(CommunityPick).where(
            CommunityPick.user_id == user_uuid,
            CommunityPick.symbol == data.symbol.upper(),
            CommunityPick.asset_type == data.asset_type.upper(),
        )
    ).first()
    if existing:
        raise ValueError("Vous avez déjà liké cet actif")

    pick = CommunityPick(
        user_id=user_uuid,
        symbol=data.symbol.upper(),
        asset_type=data.asset_type.upper(),
        comment=data.comment,
        target_price=data.target_price,
    )
    session.add(pick)
    session.commit()
    session.refresh(pick)
    return _pick_to_response(pick, user.username)


def update_pick(session: Session, user_uuid: str, pick_id: int, data: PickUpdate) -> PickResponse:
    """Update comment and/or target_price on an existing pick."""
    pick = session.exec(
        select(CommunityPick).where(
            CommunityPick.id == pick_id,
            CommunityPick.user_id == user_uuid,
        )
    ).first()
    if not pick:
        raise ValueError("Pick introuvable")

    user = session.get(User, user_uuid)
    pick.comment = data.comment
    pick.target_price = data.target_price
    session.add(pick)
    session.commit()
    session.refresh(pick)
    return _pick_to_response(pick, user.username)  # type: ignore[union-attr]


def delete_pick(session: Session, user_uuid: str, pick_id: int) -> None:
    """Delete a pick (unlike)."""
    pick = session.exec(
        select(CommunityPick).where(
            CommunityPick.id == pick_id,
            CommunityPick.user_id == user_uuid,
        )
    ).first()
    if not pick:
        raise ValueError("Pick introuvable")
    session.delete(pick)
    session.commit()


def get_user_picks(session: Session, user_uuid: str) -> List[PickResponse]:
    """Return all picks for a given user, newest first."""
    user = session.get(User, user_uuid)
    if not user:
        return []
    picks = session.exec(
        select(CommunityPick)
        .where(CommunityPick.user_id == user_uuid)
        .order_by(CommunityPick.created_at.desc())  # type: ignore[union-attr]
    ).all()
    return [_pick_to_response(p, user.username) for p in picks]


def get_picks_for_profile(session: Session, target_user_uuid: str, target_username: str) -> List[PickResponse]:
    """Return all picks for a target user — used when building public profile."""
    picks = session.exec(
        select(CommunityPick)
        .where(CommunityPick.user_id == target_user_uuid)
        .order_by(CommunityPick.created_at.desc())  # type: ignore[union-attr]
    ).all()
    return [_pick_to_response(p, target_username) for p in picks]
