from datetime import date
from pydantic import BaseModel, Field

from models.enums import AccountCategory


class ProjectionAssetParameters(BaseModel):
    monthly_injection: float | None = Field(
        None,
        description="Apport mensuel. Si vide, utilise la moyenne historique.",
    )
    return_rate: float | None = Field(
        None,
        description="Taux de rentabilite annuel (en decimal). Si vide, utilise la moyenne historique.",
    )


class ProjectionParameters(BaseModel):
    months_to_project: int = Field(
        120,
        ge=1,
        description="Nombre de mois a projeter (defaut: 120 mois / 10 ans)",
    )
    assets: dict[AccountCategory, ProjectionAssetParameters] = Field(
        default_factory=dict,
        description="Parametres par type d'actif (STOCK, CRYPTO, BANK).",
    )


class ProjectionDataPoint(BaseModel):
    date: date
    asset_values: dict[AccountCategory, float] = Field(default_factory=dict)
    total_value: float


class ProjectionAssetParametersUsed(BaseModel):
    monthly_injection: float
    return_rate: float


class ProjectionParametersUsed(BaseModel):
    months_to_project: int
    assets: dict[AccountCategory, ProjectionAssetParametersUsed]


class ProjectionResponse(BaseModel):
    parameters_used: ProjectionParametersUsed
    data: list[ProjectionDataPoint]
