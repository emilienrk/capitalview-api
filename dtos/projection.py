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


class ProjectionBasisWarning(BaseModel):
    """A reservation about a derived figure: a code, and what it hinges on."""

    code: str
    values: dict = Field(default_factory=dict)


class ProjectionAssetBasis(BaseModel):
    """Where a default came from, so a caller can state it rather than imply it."""

    contribution: str = Field(description="'net_external_flows' ou 'unavailable'")
    contribution_months: int = 0
    contribution_total: float = 0.0
    return_: str = Field(
        default="unavailable",
        alias="return",
        description="'annualised_twr' ou 'unavailable'",
    )
    return_days: int = 0
    warnings: list[ProjectionBasisWarning] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class ProjectionAssetParametersUsed(BaseModel):
    monthly_injection: float
    return_rate: float
    # Optional so no existing client breaks on a response that carries it.
    basis: ProjectionAssetBasis | None = None


class ProjectionParametersUsed(BaseModel):
    months_to_project: int
    assets: dict[AccountCategory, ProjectionAssetParametersUsed]


class ProjectionResponse(BaseModel):
    parameters_used: ProjectionParametersUsed
    data: list[ProjectionDataPoint]
