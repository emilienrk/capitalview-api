# Standardisation de l'import bancaire + désactivation des flux — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** faire passer l'import bancaire par le framework d'import unifié, et permettre de suspendre un flux de trésorerie sans perdre son mapping vers un compte bancaire.

**Architecture :** côté API, un parser `native_bank` rejoint le registry et partage sa logique avec `generic_bank` via une classe de base ; un drapeau chiffré `is_active` sur `Cashflow` et un booléen `bank_auto_sync_enabled` sur `UserSettings` filtrent `_apply_pending_cashflows`. Côté web, `Bank.vue` adopte `ImportMenu` comme Stock/Crypto, la modale d'import héritée disparaît, et un `BaseToggle` mutualisé sert les nouveaux interrupteurs.

**Tech Stack :** FastAPI + SQLModel + Alembic + pytest (`capitalview-api`) ; Vue 3 `<script setup>` + Pinia + Tailwind v4 + vitest (`capitalview-web`).

**Spec :** `capitalview-api/docs/superpowers/specs/2026-07-29-bank-import-standardization-cashflow-toggle-design.md`

## Global Constraints

- Deux repos git distincts, chacun sur la branche `feat/bank-import-standardization-cashflow-toggle`. Commits séparés par repo.
- Commentaires de code **en anglais**, densité faible, uniquement là où le *pourquoi* n'est pas évident.
- Commits en conventional commits anglais, 2-3 lignes maximum, scope quand il est évident.
- Toute donnée utilisateur de la table `cashflows` est chiffrée (`encrypt_data`/`decrypt_data` avec `master_key`). Les drapeaux de `user_settings` restent en clair, conformément à l'existant.
- Tests API : `uv run pytest` — **nécessite `dangerouslyDisableSandbox: true`** (le cache uv est bloqué par le sandbox).
- Build web : `node` n'est pas sur le PATH. Préfixer par
  `export PATH="$(dirname $(head -1 $(which pnpm) | sed 's|^#!||')):$PATH"` ou le chemin nix explicite, puis `pnpm type-check` et `pnpm test`. Éviter `pnpm build` (run-p).
- Un flux désactivé reste compté dans les totaux, le Sankey, le taux d'épargne, le dashboard et les outils IA. Seul `_apply_pending_cashflows` l'ignore.
- Aucune occurrence n'est jamais rattrapée : `balance_updated_at` avance à `today` même quand la synchro est neutralisée.

---

## Task 1 : drapeau `is_active` sur les flux de trésorerie

**Files:**
- Create: `capitalview-api/alembic/versions/ee5f6a7b8c9d_add_is_active_to_cashflows.py`
- Modify: `capitalview-api/models/cashflow.py`
- Modify: `capitalview-api/dtos/cashflow.py`
- Modify: `capitalview-api/services/cashflow.py`
- Test: `capitalview-api/tests/services/test_cashflow.py`

**Interfaces:**
- Consomme : `encrypt_data` / `decrypt_data` de `services.encryption`.
- Produit : `Cashflow.is_active_enc: str | None`, `CashflowResponse.is_active: bool`,
  `CashflowCreate.is_active: bool = True`, `CashflowUpdate.is_active: bool | None = None`.
  La Task 3 consomme `CashflowResponse.is_active`.

- [ ] **Step 1 : écrire les tests qui échouent**

Ajouter à la fin de `capitalview-api/tests/services/test_cashflow.py` (adapter les noms de fixtures à ceux déjà utilisés dans le fichier — `session`, `master_key`) :

```python
class TestCashflowIsActive:
    def test_new_cashflow_is_active_by_default(self, session: Session, master_key: str):
        created = create_cashflow(
            session,
            CashflowCreate(
                name="Salaire",
                flow_type=FlowType.INFLOW,
                category="salaire",
                amount=Decimal("3000"),
                frequency=Frequency.MONTHLY,
                transaction_date=date(2026, 1, 1),
            ),
            "is_active_user",
            master_key,
        )
        assert created.is_active is True

    def test_can_be_deactivated_and_reactivated(self, session: Session, master_key: str):
        created = create_cashflow(
            session,
            CashflowCreate(
                name="Netflix",
                flow_type=FlowType.OUTFLOW,
                category="loisirs",
                amount=Decimal("15"),
                frequency=Frequency.MONTHLY,
                transaction_date=date(2026, 1, 1),
            ),
            "toggle_user",
            master_key,
        )
        row = session.get(Cashflow, created.id)

        off = update_cashflow(session, row, CashflowUpdate(is_active=False), master_key, "toggle_user")
        assert off.is_active is False

        on = update_cashflow(session, row, CashflowUpdate(is_active=True), master_key, "toggle_user")
        assert on.is_active is True

    def test_legacy_row_without_flag_is_active(self, session: Session, master_key: str):
        created = create_cashflow(
            session,
            CashflowCreate(
                name="Loyer",
                flow_type=FlowType.OUTFLOW,
                category="logement",
                amount=Decimal("800"),
                frequency=Frequency.MONTHLY,
                transaction_date=date(2026, 1, 1),
            ),
            "legacy_user",
            master_key,
        )
        # Simulate a row created before the column existed
        row = session.get(Cashflow, created.id)
        row.is_active_enc = None
        session.add(row)
        session.commit()

        fetched = get_cashflow(session, created.id, "legacy_user", master_key)
        assert fetched.is_active is True

    def test_update_without_is_active_preserves_it(self, session: Session, master_key: str):
        created = create_cashflow(
            session,
            CashflowCreate(
                name="Prime",
                flow_type=FlowType.INFLOW,
                category="salaire",
                amount=Decimal("500"),
                frequency=Frequency.YEARLY,
                transaction_date=date(2026, 1, 1),
                is_active=False,
            ),
            "preserve_user",
            master_key,
        )
        assert created.is_active is False

        row = session.get(Cashflow, created.id)
        renamed = update_cashflow(session, row, CashflowUpdate(name="Prime annuelle"), master_key, "preserve_user")
        assert renamed.is_active is False
```

Compléter les imports en tête de fichier si nécessaire : `from models import Cashflow`, `from services.cashflow import get_cashflow, update_cashflow`, `from dtos.cashflow import CashflowUpdate`.

- [ ] **Step 2 : lancer les tests et vérifier qu'ils échouent**

```bash
cd capitalview-api && uv run pytest tests/services/test_cashflow.py::TestCashflowIsActive -v
```

Attendu : ÉCHEC — `CashflowCreate` n'accepte pas `is_active`, `Cashflow` n'a pas `is_active_enc`.

- [ ] **Step 3 : ajouter la colonne au modèle**

Dans `capitalview-api/models/cashflow.py`, après `bank_account_uuid_bidx` :

```python
    # Encrypted "true"/"false". NULL means active (rows predating the column).
    is_active_enc: str | None = Field(default=None, sa_column=Column(TEXT, nullable=True))
```

- [ ] **Step 4 : écrire la migration Alembic**

Créer `capitalview-api/alembic/versions/ee5f6a7b8c9d_add_is_active_to_cashflows.py` :

```python
"""add is_active to cashflows

Revision ID: ee5f6a7b8c9d
Revises: dd4e5f6a7b8c
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa

revision = "ee5f6a7b8c9d"
down_revision = "dd4e5f6a7b8c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL means active, so existing rows need no backfill.
    op.add_column("cashflows", sa.Column("is_active_enc", sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column("cashflows", "is_active_enc")
```

Vérifier que `dd4e5f6a7b8c` est bien la tête courante :
`cd capitalview-api && rg -l "down_revision" alembic/versions | xargs rg -n "^revision|^down_revision"` — aucune autre révision ne doit avoir `down_revision = "dd4e5f6a7b8c"`.

- [ ] **Step 5 : étendre les DTOs**

Dans `capitalview-api/dtos/cashflow.py` :

```python
class CashflowCreate(BaseModel):
    ...
    bank_account_id: str | None = None
    is_active: bool = True


class CashflowUpdate(BaseModel):
    ...
    bank_account_id: str | None = None
    is_active: bool | None = None


class CashflowResponse(BaseModel):
    ...
    bank_account_id: str | None = None  # Linked bank account UUID
    is_active: bool = True  # False = excluded from the automatic bank balance sync
```

- [ ] **Step 6 : implémenter dans le service**

Dans `capitalview-api/services/cashflow.py`, `_map_cashflow_to_response` — après la résolution de `bank_account_id` :

```python
    is_active = True
    if cashflow.is_active_enc:
        is_active = decrypt_data(cashflow.is_active_enc, master_key) == "true"
```

et ajouter `is_active=is_active,` dans le `CashflowResponse(...)`.

Dans `create_cashflow`, à côté des autres chiffrements :

```python
    is_active_enc = encrypt_data("true" if data.is_active else "false", master_key)
```

et passer `is_active_enc=is_active_enc,` au constructeur `Cashflow(...)`.

Dans `update_cashflow`, après le bloc `bank_account_id` :

```python
    if data.is_active is not None:
        cashflow.is_active_enc = encrypt_data("true" if data.is_active else "false", master_key)
```

- [ ] **Step 7 : lancer les tests et vérifier qu'ils passent**

```bash
cd capitalview-api && uv run pytest tests/services/test_cashflow.py -v
```

Attendu : tous verts, y compris les tests préexistants du fichier.

- [ ] **Step 8 : commit**

```bash
cd capitalview-api
git add models/cashflow.py dtos/cashflow.py services/cashflow.py alembic/versions/ee5f6a7b8c9d_add_is_active_to_cashflows.py tests/services/test_cashflow.py
git commit -m "feat(cashflow): add encrypted is_active flag"
```

---

## Task 2 : réglage global `bank_auto_sync_enabled`

**Files:**
- Create: `capitalview-api/alembic/versions/ff6a7b8c9d0e_add_bank_auto_sync_to_user_settings.py`
- Modify: `capitalview-api/models/user.py`
- Modify: `capitalview-api/dtos/settings.py`
- Modify: `capitalview-api/services/settings.py`
- Test: `capitalview-api/tests/routes/test_settings.py`

**Interfaces:**
- Produit : `UserSettings.bank_auto_sync_enabled: bool` (défaut `True`), exposé par
  `UserSettingsResponse.bank_auto_sync_enabled` et modifiable par `UserSettingsUpdate`.
  La Task 3 lit `get_or_create_settings(...).bank_auto_sync_enabled`.

- [ ] **Step 1 : écrire le test qui échoue**

Ajouter à `capitalview-api/tests/routes/test_settings.py` (respecter le style du fichier : il utilise un `client` et des en-têtes d'authentification déjà définis — reprendre le helper d'authentification du test voisin le plus proche) :

```python
def test_bank_auto_sync_defaults_to_true_and_can_be_disabled(client, auth_headers):
    initial = client.get("/settings", headers=auth_headers).json()
    assert initial["bank_auto_sync_enabled"] is True

    updated = client.put(
        "/settings",
        json={"bank_auto_sync_enabled": False},
        headers=auth_headers,
    ).json()
    assert updated["bank_auto_sync_enabled"] is False

    reread = client.get("/settings", headers=auth_headers).json()
    assert reread["bank_auto_sync_enabled"] is False
```

- [ ] **Step 2 : lancer le test et vérifier qu'il échoue**

```bash
cd capitalview-api && uv run pytest tests/routes/test_settings.py::test_bank_auto_sync_defaults_to_true_and_can_be_disabled -v
```

Attendu : ÉCHEC — `KeyError: 'bank_auto_sync_enabled'`.

- [ ] **Step 3 : ajouter la colonne au modèle**

Dans `capitalview-api/models/user.py`, classe `UserSettings`, juste après `bank_module_enabled` :

```python
    # False = linked cashflows no longer adjust bank balances automatically
    bank_auto_sync_enabled: bool = Field(default=True, nullable=False)
```

- [ ] **Step 4 : écrire la migration Alembic**

Créer `capitalview-api/alembic/versions/ff6a7b8c9d0e_add_bank_auto_sync_to_user_settings.py` :

```python
"""add bank_auto_sync_enabled to user_settings

Revision ID: ff6a7b8c9d0e
Revises: ee5f6a7b8c9d
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa

revision = "ff6a7b8c9d0e"
down_revision = "ee5f6a7b8c9d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("bank_auto_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "bank_auto_sync_enabled")
```

- [ ] **Step 5 : étendre DTO et service**

Dans `capitalview-api/dtos/settings.py`, `UserSettingsUpdate` (après `bank_module_enabled`) :

```python
    bank_auto_sync_enabled: bool | None = None
```

et dans `UserSettingsResponse`, au même endroit relatif :

```python
    bank_auto_sync_enabled: bool
```

Dans `capitalview-api/services/settings.py`, `_map_settings_to_response` :

```python
        bank_auto_sync_enabled=settings.bank_auto_sync_enabled,
```

et dans `update_settings`, après le bloc `bank_module_enabled` :

```python
    if data.bank_auto_sync_enabled is not None:
        settings.bank_auto_sync_enabled = data.bank_auto_sync_enabled
```

- [ ] **Step 6 : lancer les tests et vérifier qu'ils passent**

```bash
cd capitalview-api && uv run pytest tests/routes/test_settings.py tests/services/test_settings.py -v
```

Attendu : tous verts.

- [ ] **Step 7 : commit**

```bash
cd capitalview-api
git add models/user.py dtos/settings.py services/settings.py alembic/versions/ff6a7b8c9d0e_add_bank_auto_sync_to_user_settings.py tests/routes/test_settings.py
git commit -m "feat(settings): add bank_auto_sync_enabled toggle"
```

---

## Task 3 : la synchro bancaire respecte les deux interrupteurs

**Files:**
- Modify: `capitalview-api/services/bank.py:140-221`
- Test: `capitalview-api/tests/services/test_bank_auto_sync.py`

**Interfaces:**
- Consomme : `CashflowResponse.is_active` (Task 1), `UserSettings.bank_auto_sync_enabled` (Task 2).
- Produit : `_apply_pending_cashflows(session, account, cashflows, master_key, get_cashflow_occurrences_fn, auto_sync_enabled: bool)` — nouveau sixième paramètre positionnel.

- [ ] **Step 1 : écrire les tests qui échouent**

Ajouter à `capitalview-api/tests/services/test_bank_auto_sync.py`. Le helper `_link_cashflow` du fichier ne gère pas `is_active` — l'étendre d'abord :

```python
def _link_cashflow(session, master_key, account_id, amount, flow_type, frequency,
                   transaction_date, user_uuid="sync_user", is_active=True):
    return create_cashflow(
        session,
        CashflowCreate(
            name="Auto CF",
            flow_type=flow_type,
            category="test",
            amount=amount,
            frequency=frequency,
            transaction_date=transaction_date,
            bank_account_id=account_id,
            is_active=is_active,
        ),
        user_uuid,
        master_key,
    )
```

puis, en fin de fichier :

```python
class TestInactiveCashflows:
    def test_inactive_cashflow_is_ignored(self, session: Session, master_key: str):
        user_uuid = "inactive_cf_user"
        acc = _make_account(session, master_key, user_uuid=user_uuid, balance=Decimal("1000"))
        acc.balance_updated_at = date(2026, 2, 1)
        session.add(acc)
        session.commit()

        _link_cashflow(session, master_key, acc.uuid, Decimal("500"), FlowType.OUTFLOW,
                       Frequency.MONTHLY, date(2026, 1, 10), user_uuid=user_uuid, is_active=False)

        with patch("services.bank.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 21)
            summary = get_user_bank_accounts(session, user_uuid, master_key)

        assert summary.accounts[0].balance == Decimal("1000")
        session.refresh(acc)
        assert acc.balance_updated_at == date(2026, 3, 21)

    def test_active_and_inactive_mixed(self, session: Session, master_key: str):
        user_uuid = "mixed_cf_user"
        acc = _make_account(session, master_key, user_uuid=user_uuid, balance=Decimal("1000"))
        acc.balance_updated_at = date(2026, 2, 28)
        session.add(acc)
        session.commit()

        _link_cashflow(session, master_key, acc.uuid, Decimal("3000"), FlowType.INFLOW,
                       Frequency.MONTHLY, date(2026, 1, 1), user_uuid=user_uuid)
        _link_cashflow(session, master_key, acc.uuid, Decimal("1200"), FlowType.OUTFLOW,
                       Frequency.MONTHLY, date(2026, 1, 5), user_uuid=user_uuid, is_active=False)

        with patch("services.bank.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 21)
            summary = get_user_bank_accounts(session, user_uuid, master_key)

        # Only the active inflow fires on Mar 1 → 1000 + 3000
        assert summary.accounts[0].balance == Decimal("4000")

    def test_reactivation_does_not_backfill_missed_occurrences(self, session: Session, master_key: str):
        """The point of the flag: a paused period is skipped for good, never caught up."""
        user_uuid = "no_backfill_user"
        acc = _make_account(session, master_key, user_uuid=user_uuid, balance=Decimal("1000"))
        acc.balance_updated_at = date(2026, 1, 1)
        session.add(acc)
        session.commit()

        cf = _link_cashflow(session, master_key, acc.uuid, Decimal("100"), FlowType.OUTFLOW,
                            Frequency.MONTHLY, date(2026, 1, 15), user_uuid=user_uuid, is_active=False)

        # Two months pass with the cashflow disabled
        with patch("services.bank.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 20)
            get_user_bank_accounts(session, user_uuid, master_key)

        update_cashflow(session, session.get(Cashflow, cf.id),
                        CashflowUpdate(is_active=True), master_key, user_uuid)

        with patch("services.bank.date") as mock_date:
            mock_date.today.return_value = date(2026, 4, 20)
            summary = get_user_bank_accounts(session, user_uuid, master_key)

        # Only Apr 15 fires; Jan/Feb/Mar occurrences are gone for good
        assert summary.accounts[0].balance == Decimal("900")


class TestGlobalAutoSyncSwitch:
    def test_disabled_switch_freezes_balance_but_advances_stamp(self, session: Session, master_key: str):
        user_uuid = "global_off_user"
        acc = _make_account(session, master_key, user_uuid=user_uuid, balance=Decimal("1000"))
        acc.balance_updated_at = date(2026, 2, 1)
        session.add(acc)
        session.commit()

        _link_cashflow(session, master_key, acc.uuid, Decimal("500"), FlowType.OUTFLOW,
                       Frequency.MONTHLY, date(2026, 1, 10), user_uuid=user_uuid)

        update_settings(session, user_uuid, master_key,
                        UserSettingsUpdate(bank_auto_sync_enabled=False))

        with patch("services.bank.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 21)
            summary = get_user_bank_accounts(session, user_uuid, master_key)

        assert summary.accounts[0].balance == Decimal("1000")
        session.refresh(acc)
        assert acc.balance_updated_at == date(2026, 3, 21)
```

Ajouter les imports manquants en tête du fichier :

```python
from dtos.settings import UserSettingsUpdate
from models.cashflow import Cashflow
from services.cashflow import create_cashflow, update_cashflow
from services.settings import update_settings
```

(`create_cashflow` est déjà importé — ne pas le dupliquer.)

- [ ] **Step 2 : lancer les tests et vérifier qu'ils échouent**

```bash
cd capitalview-api && uv run pytest tests/services/test_bank_auto_sync.py -v
```

Attendu : les quatre nouveaux tests échouent (soldes ajustés alors qu'ils ne devraient pas l'être) ; les tests préexistants passent.

- [ ] **Step 3 : implémenter le filtrage**

Dans `capitalview-api/services/bank.py`, signature et corps de `_apply_pending_cashflows` :

```python
def _apply_pending_cashflows(
    session: Session,
    account: BankAccount,
    cashflows: list,
    master_key: str,
    get_cashflow_occurrences_fn,
    auto_sync_enabled: bool,
) -> None:
    """Apply cashflow occurrences that have fired since balance_updated_at.

    On the first call (balance_updated_at is None), we just stamp today without
    applying anything — this prevents retroactively adjusting a manually-entered balance.
    Subsequent calls apply all occurrences in (balance_updated_at, today].

    Inactive cashflows and a disabled global switch skip the balance update but
    still advance the stamp: a paused period must never be caught up later.
    """
    today = date.today()

    if account.balance_updated_at is None:
        # First run: stamp today, do not touch the balance
        account.balance_updated_at = today
        session.add(account)
        session.commit()
        return

    from_date = account.balance_updated_at
    if from_date >= today:
        return  # Already up to date

    linked = [
        cf for cf in cashflows
        if cf.bank_account_id == account.uuid and cf.is_active
    ] if auto_sync_enabled else []

    if not linked:
        account.balance_updated_at = today
        session.add(account)
        session.commit()
        return
```

Le reste de la fonction (calcul du delta, écriture du solde) est inchangé.

Dans `get_user_bank_accounts`, lire le réglage une seule fois et le propager :

```python
def get_user_bank_accounts(
    session: Session,
    user_uuid: str,
    master_key: str
) -> BankSummaryResponse:
    """Get all bank accounts for a user, applying pending cashflows first."""
    # Lazy import to avoid circular dependency
    from services.cashflow import get_all_user_cashflows, get_cashflow_occurrences
    from services.settings import get_or_create_settings

    user_bidx = hash_index(user_uuid, master_key)
    accounts = session.exec(
        select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
    ).all()

    auto_sync_enabled = get_or_create_settings(session, user_uuid, master_key).bank_auto_sync_enabled

    # Fetch cashflows once and apply pending ones to each linked account
    cashflows = get_all_user_cashflows(session, user_uuid, master_key)
    for account in accounts:
        _apply_pending_cashflows(
            session, account, cashflows, master_key, get_cashflow_occurrences, auto_sync_enabled
        )
```

Le reste de la fonction est inchangé.

- [ ] **Step 4 : lancer la suite bancaire complète**

```bash
cd capitalview-api && uv run pytest tests/services/test_bank_auto_sync.py tests/services/test_bank.py tests/routes/test_bank_routes.py -v
```

Attendu : tous verts.

- [ ] **Step 5 : commit**

```bash
cd capitalview-api
git add services/bank.py tests/services/test_bank_auto_sync.py
git commit -m "feat(bank): skip inactive cashflows and honor the global sync switch"
```

---

## Task 4 : modèle CSV téléchargeable, exposé par le framework

**Files:**
- Modify: `capitalview-api/services/imports/base.py`
- Modify: `capitalview-api/dtos/imports.py`
- Modify: `capitalview-api/services/imports/registry.py`
- Test: `capitalview-api/tests/routes/test_imports_routes.py`

**Interfaces:**
- Produit : `ImportParser.template_csv: str | None = None` et
  `ImportSourceInfo.template_csv: str | None`. La Task 5 renseigne l'attribut, la Task 7 le consomme côté web.

- [ ] **Step 1 : écrire le test qui échoue**

Ajouter à `capitalview-api/tests/routes/test_imports_routes.py` (reprendre le style d'appel et les fixtures d'authentification du fichier) :

```python
def test_sources_expose_optional_template(client, auth_headers):
    sources = client.get("/imports/sources", headers=auth_headers).json()["sources"]
    # Every source declares the field; only some carry a template.
    assert all("template_csv" in s for s in sources)
```

- [ ] **Step 2 : lancer le test et vérifier qu'il échoue**

```bash
cd capitalview-api && uv run pytest tests/routes/test_imports_routes.py::test_sources_expose_optional_template -v
```

Attendu : ÉCHEC — la clé `template_csv` est absente.

- [ ] **Step 3 : implémenter**

Dans `capitalview-api/services/imports/base.py`, classe `ImportParser`, après `supports_mapping` :

```python
    # Ready-to-fill CSV skeleton offered as a download; None = no template.
    template_csv: str | None = None
```

Dans `capitalview-api/dtos/imports.py`, `ImportSourceInfo` :

```python
    supports_mapping: bool = False
    template_csv: str | None = None
```

Dans `capitalview-api/services/imports/registry.py`, `list_parsers()` :

```python
            supports_mapping=p.supports_mapping,
            template_csv=p.template_csv,
```

- [ ] **Step 4 : lancer les tests et vérifier qu'ils passent**

```bash
cd capitalview-api && uv run pytest tests/routes/test_imports_routes.py -v
```

Attendu : tous verts.

- [ ] **Step 5 : commit**

```bash
cd capitalview-api
git add services/imports/base.py dtos/imports.py services/imports/registry.py tests/routes/test_imports_routes.py
git commit -m "feat(imports): expose an optional CSV template per source"
```

---

## Task 5 : parser bancaire « format natif »

**Files:**
- Modify: `capitalview-api/services/imports/bank_csv.py`
- Test: `capitalview-api/tests/services/test_bank_import.py`

**Interfaces:**
- Consomme : `ImportParser.template_csv` (Task 4), `parse_bank_points()` déjà présent dans le module.
- Produit : parser `source_id="native_bank"` enregistré dans le registry ; classe de base
  `_BankHistoryParser` avec le hook `effective_options(options: dict) -> dict`.

- [ ] **Step 1 : écrire les tests qui échouent**

Ajouter à `capitalview-api/tests/services/test_bank_import.py` :

```python
from services.imports.registry import get_parser

NATIVE_CSV = textwrap.dedent("""\
    snapshot_date,value
    2024-01-31,12500.00
    2024-02-29,13200.50
""")

NATIVE_FR_CSV = textwrap.dedent("""\
    snapshot_date;value
    31/01/2024;12 500,00
    29/02/2024;13200,50
""")


def test_native_parser_is_registered():
    assert get_parser("native_bank") is not None


def test_native_parser_detects_its_own_header():
    parser = get_parser("native_bank")
    assert parser.detect(NATIVE_CSV) == 1.0
    assert parser.detect("Date;Solde\n15/01/2024;1000,00\n") == 0.0


def test_native_parser_offers_a_template():
    parser = get_parser("native_bank")
    assert parser.template_csv is not None
    assert parser.template_csv.splitlines()[0] == "snapshot_date,value"


def test_native_points_parsed_without_mapping():
    parser = get_parser("native_bank")
    points, _ = parse_bank_points(NATIVE_CSV, parser.effective_options({}))
    assert [p.snapshot_date for p in points] == [date(2024, 1, 31), date(2024, 2, 29)]
    assert points[0].value == Decimal("12500.00")


def test_native_points_accept_french_dates_and_decimals():
    parser = get_parser("native_bank")
    points, _ = parse_bank_points(NATIVE_FR_CSV, parser.effective_options({}))
    assert [p.snapshot_date for p in points] == [date(2024, 1, 31), date(2024, 2, 29)]
    assert points[0].value == Decimal("12500.00")
    assert points[1].value == Decimal("13200.50")


def test_native_missing_columns_yields_no_points():
    parser = get_parser("native_bank")
    points, _ = parse_bank_points("foo,bar\n1,2\n", parser.effective_options({}))
    assert points == []
```

Les imports en tête du fichier deviennent :

```python
import textwrap
from datetime import date
from decimal import Decimal

from services.imports.bank_csv import parse_bank_points
from services.imports.registry import get_parser
```

`services.imports.bank_csv` doit être importé pour que le parser s'enregistre ; c'est déjà le cas via `parse_bank_points`.

- [ ] **Step 2 : lancer les tests et vérifier qu'ils échouent**

```bash
cd capitalview-api && uv run pytest tests/services/test_bank_import.py -v
```

Attendu : ÉCHEC — `get_parser("native_bank")` renvoie `None`.

- [ ] **Step 3 : factoriser la base commune**

Dans `capitalview-api/services/imports/bank_csv.py`, remplacer la classe `GenericBankParser` par une base et deux parsers. La docstring du module gagne une phrase sur le format natif.

```python
class _BankHistoryParser(ImportParser):
    """Shared preview/execute for bank parsers; subclasses supply the effective options."""

    category = ImportCategory.BANK

    def effective_options(self, options: dict) -> dict:
        """Options actually handed to :func:`parse_bank_points`."""
        return options

    def preview(
        self,
        session: Session,
        csv_content: str,
        options: dict,
        *,
        account_id: str | None = None,
        master_key: str | None = None,
    ) -> ImportPreviewResponse:
        points, warnings = parse_bank_points(csv_content, self.effective_options(options))

        duplicates = 0
        if account_id and master_key:
            existing = bank_existing_dates(session, account_id, master_key)
            for point in points:
                if point.snapshot_date in existing:
                    point.is_duplicate = True
                    duplicates += 1

        return ImportPreviewResponse(
            source_id=self.source_id,
            category=self.category.value,
            total_rows=len(points),
            duplicates_count=duplicates,
            warnings=warnings,
            bank_points=points,
        )

    def execute(
        self,
        session: Session,
        account_id: str,
        payload: ImportConfirmRequest,
        master_key: str,
    ) -> ImportConfirmResponse:
        from models.bank import BankAccount
        from services.bank import import_bank_account_history

        account = session.get(BankAccount, account_id)
        points = payload.bank_points or []

        entries = [
            BankHistoryEntry(snapshot_date=p.snapshot_date, value=p.value)
            for p in points
        ]
        written = import_bank_account_history(
            session, account, entries, master_key, overwrite=payload.overwrite
        )
        return ImportConfirmResponse(imported_count=written)


@register
class GenericBankParser(_BankHistoryParser):
    """Any bank statement CSV, converted into a balance curve."""

    source_id = "generic_bank"
    label = "CSV générique (relevé bancaire) avec mapping de colonnes"
    file_hint = "relevé CSV bancaire (mode solde ou mode mouvements)"
    supports_mapping = True

    def detect(self, csv_content: str) -> float:
        return 0.0  # never auto-detected


@register
class NativeBankParser(_BankHistoryParser):
    """The CSV shape CapitalView itself documents: one balance per date."""

    source_id = "native_bank"
    label = "Format CapitalView (snapshot_date, value)"
    file_hint = "CSV à deux colonnes : snapshot_date, value"
    supports_mapping = False
    template_csv = (
        "snapshot_date,value\n"
        "2024-01-31,12500.00\n"
        "2024-02-29,13200.50\n"
        "2024-03-31,11800.00\n"
    )

    _MAPPING = {"date": "snapshot_date", "balance": "value"}

    def detect(self, csv_content: str) -> float:
        header = csv_header_line(csv_content).lower()
        return 1.0 if "snapshot_date" in header and "value" in header else 0.0

    def effective_options(self, options: dict) -> dict:
        return {**options, "mapping": self._MAPPING, "bank_mode": "balance"}
```

Ajouter `csv_header_line` à l'import depuis `services.imports.base` :

```python
from services.imports.base import ImportCategory, ImportParser, csv_header_line
```

- [ ] **Step 4 : lancer les tests et vérifier qu'ils passent**

```bash
cd capitalview-api && uv run pytest tests/services/test_bank_import.py tests/routes/test_imports_routes.py -v
```

Attendu : tous verts, y compris les trois tests préexistants de `generic_bank`.

- [ ] **Step 5 : lancer la suite complète de l'API**

```bash
cd capitalview-api && uv run pytest -q
```

Attendu : aucune régression.

- [ ] **Step 6 : commit**

```bash
cd capitalview-api
git add services/imports/bank_csv.py tests/services/test_bank_import.py
git commit -m "feat(imports): add auto-detected native bank CSV parser"
```

---

## Task 6 : `BaseToggle` mutualisé et types front

**Files:**
- Create: `capitalview-web/src/components/base/BaseToggle.vue`
- Modify: `capitalview-web/src/components/index.ts`
- Modify: `capitalview-web/src/types/index.ts`
- Modify: `capitalview-web/src/pages/settings/SettingsModules.vue`
- Modify: `capitalview-web/src/pages/settings/SettingsAI.vue`
- Modify: `capitalview-web/src/pages/settings/SettingsCommunity.vue`

**Interfaces:**
- Produit : composant `BaseToggle` avec `v-model: boolean`, props `disabled?: boolean` et
  `ariaLabel?: string` ; types `CashflowResponse.is_active`, `CashflowUpdate.is_active`,
  `CashflowCreate.is_active`, `ImportSourceInfo.template_csv`,
  `UserSettingsResponse.bank_auto_sync_enabled`, `UserSettingsUpdate.bank_auto_sync_enabled`.
  Consommés par les Tasks 7 et 8.

- [ ] **Step 1 : créer le composant**

`capitalview-web/src/components/base/BaseToggle.vue` :

```vue
<script setup lang="ts">
interface Props {
  modelValue: boolean
  disabled?: boolean
  ariaLabel?: string
}

const props = withDefaults(defineProps<Props>(), {
  disabled: false,
  ariaLabel: undefined,
})

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

function toggle(): void {
  if (props.disabled) return
  emit('update:modelValue', !props.modelValue)
}
</script>

<template>
  <button
    type="button"
    role="switch"
    :aria-checked="props.modelValue"
    :aria-label="props.ariaLabel"
    :disabled="props.disabled"
    :class="[
      'relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors',
      props.modelValue ? 'bg-primary' : 'bg-surface-border dark:bg-surface-dark-border',
      props.disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer',
    ]"
    @click="toggle"
  >
    <span
      :class="[
        'inline-block h-4 w-4 transform rounded-full bg-white transition-transform shadow-sm',
        props.modelValue ? 'translate-x-6' : 'translate-x-1',
      ]"
    />
  </button>
</template>
```

- [ ] **Step 2 : l'exporter**

Dans `capitalview-web/src/components/index.ts`, ajouter la ligne au bon endroit alphabétique parmi les autres `base/` :

```ts
export { default as BaseToggle } from './base/BaseToggle.vue'
```

- [ ] **Step 3 : migrer les trois toggles existants**

Dans `SettingsModules.vue`, `SettingsAI.vue` et `SettingsCommunity.vue`, remplacer chaque bloc

```html
<button type="button" @click="X = !X" :class="['relative inline-flex h-6 w-11 items-center rounded-full transition-colors', X ? 'bg-primary' : 'bg-surface-border dark:bg-surface-dark-border']" :aria-pressed="X">
  <span :class="['inline-block h-4 w-4 transform rounded-full bg-white transition-transform shadow-sm', X ? 'translate-x-6' : 'translate-x-1']" />
</button>
```

par

```html
<BaseToggle v-model="X" :aria-label="…" />
```

en reprenant comme `aria-label` le libellé affiché à gauche du switch (par exemple `"Activer le module Crypto"`). Ajouter `BaseToggle` à l'import `from '@/components'` de chaque fichier. Repérer les occurrences avec :

```bash
cd capitalview-web && rg -n "inline-flex h-6 w-11" src/
```

Après migration cette commande ne doit plus rien renvoyer hors `BaseToggle.vue`.

Attention à `SettingsAI.vue:209` : le clic y appelle `toggleAiFeature()` et non une simple
inversion. Utiliser `:model-value="…"` + `@update:model-value="toggleAiFeature"` plutôt que
`v-model` dans ce cas précis.

- [ ] **Step 4 : étendre les types**

Dans `capitalview-web/src/types/index.ts` :

```ts
export interface CashflowCreate {
  …
  bank_account_id?: string
  is_active?: boolean
}

export interface CashflowUpdate {
  …
  bank_account_id?: string
  is_active?: boolean
}

export interface CashflowResponse {
  …
  bank_account_id: string | null
  /** false = excluded from the automatic bank balance sync */
  is_active: boolean
}

export interface ImportSourceInfo {
  …
  supports_mapping: boolean
  /** Downloadable CSV skeleton, when the source documents one. */
  template_csv: string | null
}

export interface UserSettingsUpdate {
  …
  bank_module_enabled?: boolean
  bank_auto_sync_enabled?: boolean
}

export interface UserSettingsResponse {
  …
  bank_module_enabled: boolean
  bank_auto_sync_enabled: boolean
}
```

- [ ] **Step 5 : vérifier le typage et les tests**

```bash
cd capitalview-web
export PATH="$(dirname $(sed -n '1s|^#!||p' $(which pnpm))):$PATH"
pnpm type-check && pnpm test
```

Attendu : aucune erreur de type, tests vitest verts.

- [ ] **Step 6 : commit**

```bash
cd capitalview-web
git add src/components/base/BaseToggle.vue src/components/index.ts src/types/index.ts src/pages/settings/
git commit -m "refactor(ui): extract BaseToggle and reuse it across settings"
```

---

## Task 7 : import bancaire unifié côté web

**Files:**
- Modify: `capitalview-web/src/components/imports/PlatformImportModal.vue`
- Modify: `capitalview-web/src/pages/Bank.vue`
- Delete: `capitalview-web/src/components/imports/BankHistoryImportModal.vue`

**Interfaces:**
- Consomme : `ImportSourceInfo.template_csv` et `UserSettingsResponse.bank_auto_sync_enabled` (Task 6),
  `ImportMenu` / `ImportMenuItem` de `@/components/imports/ImportMenu.vue`.
- Produit : prop `initialSourceId?: string` sur `PlatformImportModal`.

- [ ] **Step 1 : ajouter `initialSourceId` et le téléchargement du modèle**

Dans `capitalview-web/src/components/imports/PlatformImportModal.vue` :

`Props` gagne une entrée :

```ts
interface Props {
  open: boolean
  category: ImportCategory
  accounts: { id: string; name: string }[]
  accountId?: string
  initialSourceId?: string
}
```

Dans le `watch(() => props.open, …)`, remplacer la sélection par défaut :

```ts
    sources.value = await imports.sourcesFor(props.category)
    if (props.initialSourceId && sources.value.some((s) => s.source_id === props.initialSourceId)) {
      selectedSourceId.value = props.initialSourceId
    } else if (sources.value.length && !selectedSourceId.value) {
      // default to first non-generic source
      selectedSourceId.value = (sources.value.find((s) => !s.supports_mapping) ?? sources.value[0]!).source_id
    }
```

Ajouter la fonction de téléchargement à côté des autres helpers :

```ts
function downloadTemplate() {
  const template = selectedSource.value?.template_csv
  if (!template) return
  const url = URL.createObjectURL(new Blob([template], { type: 'text/csv' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `modele_${selectedSource.value!.source_id}.csv`
  link.click()
  URL.revokeObjectURL(url)
}
```

Importer `Download` depuis `lucide-vue-next` (ligne 2 : `import { Download, Upload, Wand2 } from 'lucide-vue-next'`).

Dans le template, bloc « Source » de l'étape 1, remplacer le `<label>` seul par un en-tête à deux éléments :

```html
        <div>
          <div class="flex items-center justify-between mb-1">
            <label class="block text-sm font-medium text-text-main dark:text-text-dark-main">Plateforme / format</label>
            <button
              v-if="selectedSource?.template_csv"
              type="button"
              class="inline-flex items-center gap-1.5 text-xs font-medium text-primary hover:text-primary-hover transition-colors"
              @click="downloadTemplate"
            >
              <Download class="w-3.5 h-3.5" />
              Modèle
            </button>
          </div>
          <select v-model="selectedSourceId" …>
```

(le `<select>` et le `<p>` du `file_hint` restent inchangés).

- [ ] **Step 2 : brancher `ImportMenu` dans `Bank.vue`**

Dans `capitalview-web/src/pages/Bank.vue`, script :

```ts
import { FileSpreadsheet, Pencil, RefreshCw, Upload } from 'lucide-vue-next'
```

Supprimer l'import de `BankHistoryImportModal` et ajouter :

```ts
import ImportMenu, { type ImportMenuItem } from '@/components/imports/ImportMenu.vue'
import { useSettingsStore } from '@/stores/settings'
```

Remplacer les deux refs d'état d'import :

```ts
const showPlatformImportModal = ref(false)
const platformImportAccountId = ref('')
const importSourceId = ref('')
```

(`showHistoryImportModal` est supprimé, ainsi que la fonction `handleHistoryImported`.)

Ajouter, à côté des autres constantes :

```ts
const settingsStore = useSettingsStore()

const IMPORT_MENU_ITEMS: ImportMenuItem[] = [
  { key: 'native_bank', label: 'Format CapitalView', icon: FileSpreadsheet },
  { key: 'generic_bank', label: 'Relevé bancaire', icon: Upload },
]

function onImportMenuSelect(key: string): void {
  importSourceId.value = key
  showPlatformImportModal.value = true
}
```

- [ ] **Step 3 : mettre à jour le template de `Bank.vue`**

Les actions de `PageHeader` deviennent :

```html
      <template #actions>
        <ImportMenu
          :items="IMPORT_MENU_ITEMS"
          :disabled="!bank.summary?.accounts?.length"
          @select="onImportMenuSelect"
        />
        <BaseAddButton @click="openCreate">Nouveau compte</BaseAddButton>
      </template>
```

Le bloc `<BankHistoryImportModal … />` est supprimé. `PlatformImportModal` reçoit la source :

```html
    <PlatformImportModal
      v-if="bank.summary?.accounts?.length"
      :open="showPlatformImportModal"
      category="bank"
      :accounts="bank.summary.accounts"
      :initial-source-id="importSourceId"
      v-model:accountId="platformImportAccountId"
      @close="showPlatformImportModal = false"
      @imported="handlePlatformImported"
    />
```

Ajouter, juste après le `BaseAlert` d'erreur existant, l'information de synchro coupée :

```html
    <BaseAlert v-if="settingsStore.settings && !settingsStore.settings.bank_auto_sync_enabled" variant="info" class="mb-6">
      Synchronisation automatique désactivée : les flux liés n'ajustent plus les soldes.
    </BaseAlert>
```

- [ ] **Step 4 : supprimer la modale héritée**

```bash
cd capitalview-web && git rm src/components/imports/BankHistoryImportModal.vue
rg -n "BankHistoryImportModal" src/
```

La recherche ne doit plus rien renvoyer.

- [ ] **Step 5 : vérifier le typage et les tests**

```bash
cd capitalview-web
export PATH="$(dirname $(sed -n '1s|^#!||p' $(which pnpm))):$PATH"
pnpm type-check && pnpm test
```

Attendu : aucune erreur.

- [ ] **Step 6 : commit**

```bash
cd capitalview-web
git add src/components/imports/PlatformImportModal.vue src/pages/Bank.vue src/components/imports/BankHistoryImportModal.vue
git commit -m "refactor(bank): route CSV imports through the unified import modal"
```

---

## Task 8 : interrupteurs côté flux et réglages

**Files:**
- Modify: `capitalview-web/src/pages/Cashflow.vue`
- Modify: `capitalview-web/src/pages/settings/SettingsFinances.vue`

**Interfaces:**
- Consomme : `BaseToggle` et les types de la Task 6, `cashflow.updateCashflow(id, data)` du store existant.

- [ ] **Step 1 : colonne « Compte lié » et toggle dans le tableau des flux**

Dans `capitalview-web/src/pages/Cashflow.vue`, script — ajouter `BaseToggle` à l'import
`from '@/components'`, puis à côté des autres computed :

```ts
const bankAccountNameById = computed(() => {
  const map: Record<string, string> = {}
  for (const account of bank.summary?.accounts ?? []) map[account.id] = account.name
  return map
})

async function toggleActive(item: CashflowResponse, value: boolean): Promise<void> {
  await cashflow.updateCashflow(item.id, { is_active: value })
}
```

Dans le `<thead>` du tableau, insérer un en-tête « Compte lié » entre « Fréquence » et le
premier en-tête aligné à droite :

```html
              <th class="text-left px-6 py-3 text-xs font-semibold text-text-muted dark:text-text-dark-muted uppercase tracking-wider">Compte lié</th>
```

Dans le `<tr v-for="item in filteredCashflows">`, ajouter la classe d'atténuation :

```html
            <tr
              v-for="item in filteredCashflows"
              :key="item.id"
              :class="[
                'hover:bg-surface-hover dark:hover:bg-surface-dark-hover transition-colors',
                item.is_active ? '' : 'opacity-60',
              ]"
            >
```

et la cellule correspondante, à la même position que l'en-tête :

```html
              <td class="px-6 py-4">
                <BaseBadge v-if="item.bank_account_id" variant="secondary">
                  {{ bankAccountNameById[item.bank_account_id] ?? 'Compte supprimé' }}
                </BaseBadge>
                <span v-else class="text-sm text-text-muted dark:text-text-dark-muted">—</span>
              </td>
```

Dans la cellule Actions, avant le bouton d'édition :

```html
                  <BaseToggle
                    v-if="item.bank_account_id"
                    :model-value="item.is_active"
                    :aria-label="`Synchroniser ${item.name} avec le compte bancaire`"
                    @update:model-value="toggleActive(item, $event)"
                  />
```

- [ ] **Step 2 : carte « Synchronisation bancaire » dans les réglages**

Dans `capitalview-web/src/pages/settings/SettingsFinances.vue`, script — ajouter `BaseToggle`
à l'import `from '@/components'`, `RefreshCw` à l'import `lucide-vue-next`, puis :

```ts
const bankAutoSync = ref(true)

async function saveBankAutoSync(value: boolean): Promise<void> {
  bankAutoSync.value = value
  await settingsStore.updateSettings({ bank_auto_sync_enabled: value })
}
```

et dans le `onMounted` existant, à côté des autres affectations :

```ts
    bankAutoSync.value = settingsStore.settings.bank_auto_sync_enabled
```

Dans le template, ajouter une `BaseCard` au même niveau que les autres cartes de la page :

```html
    <BaseCard class="mb-6">
      <template #header>
        <div class="flex items-center gap-2">
          <RefreshCw class="w-5 h-5 text-primary" />
          <h3 class="text-lg font-semibold text-text-main dark:text-text-dark-main">Synchronisation bancaire</h3>
        </div>
      </template>
      <div class="flex items-center justify-between gap-4">
        <div>
          <p class="font-medium text-text-main dark:text-text-dark-main">Appliquer les flux aux soldes bancaires</p>
          <p class="text-sm text-text-muted dark:text-text-dark-muted">
            Chaque revenu ou dépense lié à un compte ajuste automatiquement son solde à échéance.
            Désactivé, les soldes ne bougent plus et les échéances passées ne sont pas rattrapées.
          </p>
        </div>
        <BaseToggle
          :model-value="bankAutoSync"
          aria-label="Appliquer les flux aux soldes bancaires"
          @update:model-value="saveBankAutoSync"
        />
      </div>
    </BaseCard>
```

- [ ] **Step 3 : vérifier le typage et les tests**

```bash
cd capitalview-web
export PATH="$(dirname $(sed -n '1s|^#!||p' $(which pnpm))):$PATH"
pnpm type-check && pnpm test
```

Attendu : aucune erreur.

- [ ] **Step 4 : commit**

```bash
cd capitalview-web
git add src/pages/Cashflow.vue src/pages/settings/SettingsFinances.vue
git commit -m "feat(cashflow): toggle bank sync per flow and globally"
```

---

## Vérification finale

- [ ] `cd capitalview-api && uv run pytest -q` — suite complète verte.
- [ ] `cd capitalview-web && pnpm type-check && pnpm test` — verts.
- [ ] `rg -n "BankHistoryImportModal|inline-flex h-6 w-11" capitalview-web/src` — ne renvoie que `BaseToggle.vue`.
- [ ] Les deux repos ont leurs commits sur `feat/bank-import-standardization-cashflow-toggle`.
