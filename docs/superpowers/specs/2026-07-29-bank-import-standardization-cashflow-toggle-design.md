# Standardisation de l'import bancaire + désactivation des flux

Date : 2026-07-29
Portée : `capitalview-api` + `capitalview-web`

## Problème

**1. L'import bancaire est hors framework.** `pages/Bank.vue` expose deux boutons bruts
(« Importer », « Relevé ») alors que Crypto et Stock passent par `ImportMenu`. Le bouton
« Importer » ouvre `BankHistoryImportModal.vue`, une modale héritée qui parse le CSV côté
client (détection de délimiteur, normalisation des décimales et des dates réimplémentées en
JavaScript) et appelle directement `POST /bank/accounts/{id}/history/import`. Elle ignore
donc `services/imports/registry.py` : pas de détection de source, pas de preview serveur,
pas de détection de doublons. Côté back, la catégorie `bank` n'a qu'un seul parser
(`generic_bank`, mapping manuel obligatoire) — aucun équivalent auto-détecté du format
natif `snapshot_date,value`.

**2. On ne peut pas suspendre un flux sans casser son mapping.** `_apply_pending_cashflows`
(`services/bank.py`) applique les occurrences de tous les flux liés à un compte à chaque
appel de `get_user_bank_accounts`. Le seul moyen d'arrêter cet impact est de délier le
compte (`bank_account_id = ''`), ce qui perd l'association et oblige à re-mapper le flux
plus tard. Or un revenu ou une dépense s'interrompt souvent temporairement.

## Décisions

- Un flux « désactivé » **cesse uniquement d'impacter le solde bancaire**. Il reste compté
  dans les totaux, le Sankey, le taux d'épargne, le dashboard et les outils IA.
- Deux niveaux de contrôle : un interrupteur par flux, et un interrupteur global dans les
  réglages qui coupe toute la synchro automatique.
- La modale d'import héritée disparaît ; la route `POST /bank/accounts/{id}/history/import`
  est conservée (elle reste le point d'entrée d'`import_bank_account_history`).

## A. Import bancaire

### A1. Parser « format natif » (`capitalview-api`)

Nouveau `NativeBankParser` dans `services/imports/bank_csv.py`, enregistré via `@register` :

| Attribut | Valeur |
| --- | --- |
| `source_id` | `native_bank` |
| `label` | `Format CapitalView (snapshot_date, value)` |
| `category` | `ImportCategory.BANK` |
| `supports_mapping` | `False` |
| `template_csv` | en-tête + 3 lignes d'exemple |

`detect()` renvoie `1.0` si l'en-tête contient `snapshot_date` **et** `value`, sinon `0.0`.

`preview()` et `execute()` réutilisent `parse_bank_points()` avec un mapping figé
`{"date": "snapshot_date", "balance": "value"}`. Aucune logique de parsing dupliquée :
`read_rows()` sniffe le délimiteur, `parse_generic_date()` accepte ISO et `DD/MM/YYYY`,
`parse_generic_decimal()` accepte virgule et point. La détection de doublons via
`bank_existing_dates()` est héritée du chemin standard — la modale héritée ne l'avait pas.

Pour éviter la duplication entre les deux parsers bancaires, la logique commune de
`preview` / `execute` est factorisée dans une base `_BankParserMixin` (ou fonctions
partagées au niveau module) dont `GenericBankParser` et `NativeBankParser` héritent ; seuls
les métadonnées et le mapping diffèrent.

### A2. Modèle CSV téléchargeable, générique

Le téléchargement du modèle existe aujourd'hui dans la modale héritée. Il est remonté au
niveau du framework plutôt que réimplémenté :

- `ImportParser.template_csv: str | None = None` (`services/imports/base.py`).
- `ImportSourceInfo.template_csv: str | None` (`dtos/imports.py`), rempli par
  `registry.list_parsers()`.
- Côté web, `PlatformImportModal` affiche un lien « Modèle » (icône `Download`) à côté du
  sélecteur de source quand la source sélectionnée déclare un `template_csv` ; le clic
  télécharge un `Blob` nommé `modele_<source_id>.csv`.

N'importe quel parser futur peut en bénéficier sans toucher au front.

### A3. `Bank.vue` aligné sur `Stock.vue`

Les deux boutons sont remplacés par un `ImportMenu` unique :

```
[ Importer ▾ ]
  ├ Format CapitalView     → PlatformImportModal, source native_bank
  └ Relevé bancaire        → PlatformImportModal, source generic_bank
```

`PlatformImportModal` reçoit une nouvelle prop optionnelle `initialSourceId` : quand elle
est fournie, elle prime sur la sélection par défaut et sur la détection automatique n'a
lieu qu'après le choix du fichier (la détection peut toujours corriger la source).

`BankHistoryImportModal.vue` est supprimé.

## B. Désactivation d'un flux

### B1. Modèle et migration (`capitalview-api`)

- `cashflows.is_active_enc TEXT NULL` — chiffré comme le reste de la table.
  `NULL` signifie « actif » : aucun backfill nécessaire sur les lignes existantes.
- `user_settings.bank_auto_sync_enabled BOOLEAN NOT NULL DEFAULT true` — booléen en clair,
  cohérent avec les autres drapeaux de `UserSettings`.

Deux révisions Alembic distinctes, dans le style des migrations existantes.

### B2. Service

- `_map_cashflow_to_response()` déchiffre `is_active_enc` (absent ⇒ `True`) et renseigne
  `CashflowResponse.is_active`.
- `create_cashflow()` / `update_cashflow()` chiffrent `data.is_active` quand il est fourni.
  `CashflowCreate.is_active` vaut `True` par défaut ; `CashflowUpdate.is_active` est
  `bool | None`.
- `_apply_pending_cashflows()` :
  - sort tôt si `bank_auto_sync_enabled` est faux ;
  - filtre `linked` sur `cf.is_active` ;
  - **dans les deux cas, estampille `balance_updated_at = today` avant de sortir.**

Ce dernier point est la décision structurante : sans elle, réactiver un flux après trois
mois d'interruption appliquerait les trois mois d'occurrences d'un coup au solde.
Désactiver signifie « ces occurrences ne passeront jamais », pas « elles sont en attente ».

Le réglage global est lu une fois par appel de `get_user_bank_accounts()` et passé en
argument à `_apply_pending_cashflows()`, pour ne pas requêter les settings par compte.

### B3. API

Aucune nouvelle route : `PUT /cashflow/{id}` accepte déjà des mises à jour partielles via
`CashflowUpdate`, et `PUT /settings` gère `bank_auto_sync_enabled` via `UserSettingsUpdate`.

## C. Frontend

### C1. `BaseToggle.vue`

Le markup du switch est dupliqué à l'identique dans `SettingsModules.vue`, `SettingsAI.vue`
et `SettingsCommunity.vue`. Il devient `components/base/BaseToggle.vue` :
`v-model` booléen, props `disabled` et `ariaLabel`, `role="switch"` et `aria-checked`
corrects. Les trois occurrences existantes sont migrées, et le composant est exporté depuis
`components/index.ts`.

### C2. Page Flux de trésorerie

- Nouvelle colonne « Compte lié » : le compte bancaire associé n'est visible nulle part
  aujourd'hui, il faut ouvrir la modale d'édition pour le connaître. `BaseBadge` avec le nom
  du compte, `—` sinon.
- `BaseToggle` dans la colonne Actions, avec mise à jour optimiste via
  `cashflow.updateCashflow(id, { is_active })`.
- Ligne inactive : `opacity-60`, pour que l'état se lise d'un coup d'œil.
- Le toggle n'apparaît que si le flux est lié à un compte — sans lien, il n'a aucun effet.

### C3. Réglages

`SettingsFinances.vue` reçoit une carte « Synchronisation bancaire » avec le `BaseToggle`
global, décrivant l'effet : les flux liés cessent d'ajuster automatiquement les soldes.

### C4. `Bank.vue`

`BaseAlert` d'information quand `bank_auto_sync_enabled` est faux : sans ça, l'utilisateur
voit ses badges « Sync le … » figés sans comprendre pourquoi.

## Tests

`capitalview-api`, tests unitaires (pytest) :

- `tests/services/test_bank_import.py` — détection du format natif (score 1.0, et 0.0 sur un
  CSV étranger), parsing d'un fichier natif ISO et d'un fichier `DD/MM/YYYY` à virgule
  décimale, en-tête sans les colonnes requises.
- `tests/services/test_bank_auto_sync.py` — flux inactif ignoré ; réactivation sans
  rattrapage des occurrences de la période d'inactivité ; switch global à `false` neutralisant
  tous les comptes tout en avançant `balance_updated_at` ; mélange flux actif / inactif sur
  un même compte.
- `tests/services/test_cashflow.py` — `is_active` par défaut à `True`, bascule via
  `update_cashflow`, valeur `True` pour les lignes héritées où `is_active_enc` est `NULL`.

`capitalview-web` : pas de suite de tests dans le repo ; vérification par `pnpm build`
(type-check inclus) et revue manuelle.
