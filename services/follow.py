"""Follow service — manage follow relationships between users."""

from sqlmodel import Session, select, func

from models.community import CommunityFollow, CommunityProfile
from models.user import User
from dtos.community import FollowResponse


def is_following(session: Session, follower_id: str, following_id: str) -> bool:
    """Check if follower_id follows following_id."""
    return session.exec(
        select(CommunityFollow).where(
            CommunityFollow.follower_id == follower_id,
            CommunityFollow.following_id == following_id,
        )
    ).first() is not None


def is_mutual(session: Session, user_a: str, user_b: str) -> bool:
    """Check if two users mutually follow each other."""
    return is_following(session, user_a, user_b) and is_following(session, user_b, user_a)


def follow_user(session: Session, follower_id: str, target_username: str) -> FollowResponse:
    """Follow a user by username. Returns the new follow state."""
    target = session.exec(select(User).where(User.username == target_username)).first()
    if not target:
        raise ValueError("Utilisateur introuvable")
    if target.uuid == follower_id:
        raise ValueError("Vous ne pouvez pas vous suivre vous-même")

    # Check the target has an active community profile
    profile = session.exec(
        select(CommunityProfile).where(
            CommunityProfile.user_id == target.uuid,
            CommunityProfile.is_active == True,  # noqa: E712
        )
    ).first()
    if not profile:
        raise ValueError("Ce profil n'est pas actif")

    existing = session.exec(
        select(CommunityFollow).where(
            CommunityFollow.follower_id == follower_id,
            CommunityFollow.following_id == target.uuid,
        )
    ).first()

    if existing:
        # Already following
        mutual = is_following(session, target.uuid, follower_id)
        return FollowResponse(is_following=True, is_mutual=mutual)

    follow = CommunityFollow(follower_id=follower_id, following_id=target.uuid)
    session.add(follow)
    session.commit()

    mutual = is_following(session, target.uuid, follower_id)
    return FollowResponse(is_following=True, is_mutual=mutual)


def unfollow_user(session: Session, follower_id: str, target_username: str) -> FollowResponse:
    """Unfollow a user by username."""
    target = session.exec(select(User).where(User.username == target_username)).first()
    if not target:
        raise ValueError("Utilisateur introuvable")

    existing = session.exec(
        select(CommunityFollow).where(
            CommunityFollow.follower_id == follower_id,
            CommunityFollow.following_id == target.uuid,
        )
    ).first()

    if existing:
        session.delete(existing)
        session.commit()

    return FollowResponse(is_following=False, is_mutual=False)


def get_followers_count(session: Session, user_id: str) -> int:
    """Count how many users follow user_id."""
    result = session.exec(
        select(func.count()).where(CommunityFollow.following_id == user_id)
    ).one()
    return result


def get_following_count(session: Session, user_id: str) -> int:
    """Count how many users user_id follows."""
    result = session.exec(
        select(func.count()).where(CommunityFollow.follower_id == user_id)
    ).one()
    return result


def get_follow_state(session: Session, current_user_id: str, target_user_id: str) -> dict:
    """Return follow state between current user and target."""
    i_follow = is_following(session, current_user_id, target_user_id)
    they_follow = is_following(session, target_user_id, current_user_id)
    return {
        "is_following": i_follow,
        "is_followed_by": they_follow,
        "is_mutual": i_follow and they_follow,
    }
