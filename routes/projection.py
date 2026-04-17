from fastapi import APIRouter, Depends
from sqlmodel import Session

from database import get_session
from models.user import User
from services.auth import get_current_user, get_master_key
from dtos.projection import ProjectionParameters, ProjectionResponse
from services.projection import generate_wealth_projection

router = APIRouter(prefix="/projections", tags=["Projections"])

@router.post("/calculate", response_model=ProjectionResponse)
def calculate_projection(
    params: ProjectionParameters,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    master_key: str = Depends(get_master_key)
):
    """
    Calcule la projection du patrimoine dans le futur en simulant 
    l'évolution pour les banques, la bourse et la crypto.
    Les paramètres manquants utiliseront les moyennes historiques de l'utilisateur.
    """
    return generate_wealth_projection(session=session, user=current_user, master_key=master_key, params=params)
