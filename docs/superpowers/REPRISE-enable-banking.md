# Reprise du chantier Enable Banking — état au 2026-08-25

Ce document est autosuffisant : il dit où en est le travail, ce qui a été décidé et pourquoi, et ce
qu'il reste à faire. Il est suivi par git et voyage avec la branche.

> **Le ledger détaillé est dans `.superpowers/sdd/2026-08-17-enable-banking-connection/progress.md`,
> qui est git-ignoré et donc LOCAL à la machine d'origine.** Il contient l'historique complet, les
> rapports de chaque tâche et les paquets de revue. Si tu reprends sur une autre machine, ce document-ci
> est ta seule source ; s'il est disponible, le ledger le complète.

## Documents de référence

| Quoi | Où |
| --- | --- |
| Plan d'implémentation (12 tâches) | `docs/superpowers/plans/2026-08-17-enable-banking-connection.md` |
| Spec de conception (le pourquoi) | `docs/superpowers/specs/2026-08-17-enable-banking-connection-design.md` |
| Contrat d'API figé | `.superpowers/sdd/2026-08-17-enable-banking-connection/api-contract.md` (local) |
| Contraintes globales | `.superpowers/sdd/2026-08-17-enable-banking-connection/constraints.md` (local) |
| Doc vendeur, contrat OpenAPI, jeux de données réels | `../vendor-docs/` (hors dépôt git) |

**Méthode employée :** `superpowers:subagent-driven-development` — un sous-agent implémenteur par tâche,
une revue après chaque tâche, des rounds de correction, puis une revue finale de branche.
Tous les dispatches sur **Opus 5** (préférence explicite d'Emilien ; Sonnet réservé au trivial).

## État

Branche `feat/enable-banking-connection` dans **les deux dépôts**.

| Tâche | État | Commit |
| --- | --- | --- |
| 1 — identifiants BYO chiffrés | ✅ revue clean | `801d4d9` |
| 2 — client Enable Banking | ✅ après 1 round | `a313e79` |
| 3 — tables session/lien/autorisation | ✅ après 1 round | `01a72a0` |
| 4 — parcours de liaison | ✅ après 1 round | `6b1b48b` |
| 5 — transactions + déduplication | ✅ après 1 round | `502dc61` |
| 6 — synchro, ancres, réconciliation | ⚠️ implémentée, **re-revue jamais faite** | `6fae804` |
| 9+10 — front (groupées) | ✅ revue clean (2 rounds) | `26e6825` (web) |
| R18 front — alerte pilotée par `reconciliation_status` | ⚠️ implémentée, **non revue** | `8b83791` (web) |
| 7+8 — consentements + suppression compte | ⚠️ implémentées, **non revues** | `d77a48f` |
| 11 — import d'un export | ⚠️ implémentée, **non revue** | `17d6de5` |
| 12 — validation à l'échelle réelle | ⚠️ implémentée, **non revue**, et les 2 ⚠️ non levés | `4d53304` |
| Revue finale de branche | ❌ à faire, **sur Opus, sur tout le diff** | — |
| 13 — opt-in `open_banking_enabled` + listing des connexions | ⚠️ implémentée, **non revue** | voir ci-dessous |

Suite API : **1061 tests verts** (vérifiés le 25/08). Front : `pnpm type-check` propre, **85 tests**.

## Ajout du 2026-08-26 — la fonctionnalité devient opt-in

Demandé par Emilien : la connexion d'un vrai compte bancaire ne doit pas être un élément principal
de l'app, tout en restant faisable. Ajouté par-dessus les 12 tâches, **non revu comme le reste**.

- **`user_settings.open_banking_enabled`**, défaut `false`, migration `c4b81e07d5a3`. Distinct de
  `bank_module_enabled` (défaut `true`, qui n'affiche/masque que le module Banque) — ne pas confondre.
- **`require_open_banking`** (`routes/banking.py`) refuse en 403 tout ce qui **ouvre, prolonge ou
  alimente** une connexion : `PUT /credentials`, `GET /aspsps`, `POST /authorize`,
  `GET|POST /sessions/{uuid}/…`, `POST /sync`, `POST /import-export`.
  **Délibérément non appliqué** à `GET /status`, `GET /check`, `GET /sessions`,
  `DELETE /sessions/{uuid}` et au callback : se désactiver doit laisser voir et démonter ce qui est
  déjà rattaché, et ne doit pas abandonner un parcours en vol chez la banque.
- **`GET /banking/sessions`** (`list_bank_sessions`) : une ligne par autorisation, avec son statut,
  son message, sa date d'expiration et les comptes CapitalView rattachés. Les sessions retirées y
  restent — leurs `BankAccountLink` survivent à l'expiration par conception.
- Front : toggle en tête de l'onglet Banque (patron `ai_feature_enabled`), tout le reste derrière ;
  panneau « Connexions bancaires » affiché **même feature éteinte** ; boutons « Connecter une banque »
  et « Synchroniser » retirés de `Bank.vue` et auto-synchro coupée quand l'opt-in est éteint.
- Tests : +10 dans `tests/routes/test_banking_linking.py`, dont le gate **prouvé non-vide par
  mutation** (retirer la dépendance de `POST /sync` rend le cas rouge). Suite API **1095 verts**,
  front type-check propre et 85 tests.
- **Non fait, volontairement** : aucune UI de déconnexion (`DELETE /sessions/{uuid}` reste non
  appelée côté front — ruling R3), aucune UI d'import d'export.

## ⚠️ Mise à jour du 2026-08-25 — implémentation complète, revue absente

Les 12 tâches sont implémentées. **Suite API : 1061 tests verts** (relancés et vérifiés le 25/08).
Front : type-check propre, 85 tests. Arbres de travail propres dans les deux dépôts.

**Mais rien de ce qui a été produit après le 17/08 n'a été revu**, et le process de revue est ce qui a
trouvé tous les défauts graves de ce chantier. Concrètement, il manque :

1. **La re-revue de la Task 6.** Elle n'a jamais tourné. Le correctif `6fae804` **change la sémantique
   de `anchor_balance_enc`** — de « le solde au moment où on a regardé » à « le solde de clôture de la
   veille ». Personne n'a vérifié la cohérence entre synchros successives (l'ancre de clôture de N doit
   être l'ancre d'ouverture de N+1), le cas d'amorçage, ni les autres lecteurs du champ.
2. **Tasks 7, 8, 11, 12 — aucune revue, aucun rapport.** Commits `d77a48f`, `17d6de5`, `4d53304`.
3. **La revue finale de branche**, et le tri des ~25 findings Minor différés.
4. **Les deux ⚠️ ne sont pas levés.** `tests/integration/test_banking_e2e.py:143` code
   `"cash_account_type": "CARD"` **en dur dans un double** : le test *suppose* ce qu'il fallait
   *vérifier*. Aucune capture nouvelle dans `vendor-docs/spike/`, aucun `GET /aspsps`. R12, R18 et R19
   reposent toujours sur un champ jamais confronté à la vraie banque.
5. **Aucune des deux branches n'est poussée.**

**Vert ≠ correct.** La Task 5 l'a démontré : son test de transition en attente→comptabilisée passait sur
une forme de donnée que la banque n'a jamais produite en 294 lignes réelles.

## Premier geste à la reprise

**Lancer la revue finale de branche** (Opus, sur tout le diff `main..HEAD`), puis la re-revue de la Task 6 sur `60d764d..6fae804`. Elle a été coupée en plein travail.
Le paquet de revue existe déjà (`review-60d764d..6fae804.diff`) si le workspace local est intact ;
sinon le régénérer avec `git diff -U10 60d764d..6fae804`.

Ce qu'elle devait juger en priorité : la **nouvelle sémantique d'ancre**. Le correctif est allé plus
loin que prescrit — `anchor_balance_enc` ne signifie plus « le solde au moment où on a regardé » mais
« le solde de clôture de la veille » (`comptable − mouvements comptabilisés aujourd'hui`). À vérifier :
la soustraction dans les deux sens, la cohérence entre synchros successives (l'ancre de clôture de la
période N doit être l'ancre d'ouverture de N+1, sans jour compté deux fois ni sauté), le cas de la
passe d'amorçage sans ancre précédente, le cas « aucun mouvement aujourd'hui », et tout autre lecteur
de `anchor_balance_enc` / `anchor_date`.

Ensuite : les revues manquantes des Tasks 7+8, 11, 12 et du commit front `8b83791`, puis le tri des
findings Minor différés. **Il ne reste aucune implémentation à écrire — seulement à vérifier.**

## Les rulings — décisions prises au nom d'Emilien

Chacun est une décision que le plan ou la spec ne tranchait pas. **Ils sont tous rejouables et réversibles.**

| # | Décision | Coût si faux |
| --- | --- | --- |
| R1 | Le spike est `vendor-docs/spike/eb_spike.py`, pas `scratchpad/` (chemin faux dans le plan) | nul |
| R2 | Le client s'obtient par une fabrique module-level `build_client(...)` que les tests monkeypatchent, pas par un conteneur d'injection | un point d'appel à refactorer |
| R3 | Le code de fermeture de session Enable Banking est écrit mais **jamais exercé pour de vrai** | un consentement fermé = nouvelle authentification forte |
| R4 | La validation « bout en bout » (Task 12) se fait **hors réseau**, en rejouant les jeux réels locaux | le parcours réseau réel reste couvert par la seule validation manuelle du 17/08 |
| R5 | Les `BankAccountLink` sont créés à l'étape de rattachement, pas au callback (`bank_account_uuid_bidx` est UNIQUE vers un compte CapitalView qui doit exister d'abord) | une étape d'UI en plus ou en moins |
| R6 | `BankAccountResponse` porte les métadonnées de lien lues par le front | le champ atterrit ailleurs |
| R7 | Le marquage « estimé » est **dérivé** de `anchor_date`, pas stocké | une colonne à ajouter |
| R8 | Les consommateurs de `get_connection()` sont T4 et T6, pas T2/T5 comme l'annonçait le plan | nul |
| R9 | Branches créées dans les deux dépôts depuis `main` | nul |
| **R10** | **`bank_sessions.accounts_enc`** : le callback persiste la charge complète des comptes. `POST /sessions` renvoie des `AccountResource` riches, `GET /sessions/{id}` ne renvoie que `{uid, identification_hash, identification_hashes}` — le §C4 « fourni une seule fois » était littéralement inversé | une table à extraire si ces métadonnées devaient un jour être interrogées champ par champ |
| **R11** | **La devise entre dans `dedup_bidx`**, contre la lettre de §A5. La donnée réelle contient un débit CHF 12,63 ; un EUR 12,63 le même jour aurait partagé son empreinte et une opération aurait disparu | empreinte plus discriminante que prévu — comportement correct de toute façon |
| R12 | **Ordre de synchro stable, compte courant avant compte carte.** La dédup croisée est asymétrique : le compte synchronisé en second perd 197 de ses 297 mouvements | la courbe d'un des deux comptes est fausse |
| R13 | Un Minor (`ValueError` sur sens/statut manquants) ajouté au fix round de T5 car il partageait la mécanique du Critical | périmètre de fix un peu large |
| R14 | Le contrat d'API est **figé** dans `api-contract.md` et fait autorité **dans les deux sens** — c'est ce qui a permis de développer le front avant la synchro | le front s'ajuste sur les champs concernés |
| R15 | Parallélisation **entre les deux dépôts**, jamais à l'intérieur d'un dépôt (deux implémenteurs sur une même branche git s'écrasent) | aucun |
| R16 | **`POST /banking/sync` : corps vide, déclencheur global.** Un déclencheur par compte rendrait l'ordre de R12 au front, c'est-à-dire à personne | une route par compte s'ajouterait sans casser celle-ci |
| R17 | Quatre Minor ajoutés au fix round front (dont deux qui racontaient une contre-vérité à l'utilisateur) | périmètre un peu large |
| **R18** | **La réconciliation a trois issues** : `reconciled` / `gap` / `not_reconcilable`. Un compte carte a un écart **permanent et normal** ; une alerte permanente sur un fonctionnement normal détruit le signal sur *tous* les comptes | un état d'affichage à retirer |
| **R19** | **Sur un compte non réconciliable, la synchro n'écrit AUCUNE courbe rétrospective.** La borne d'amorçage remontait à la plus ancienne ligne *envoyée par la banque*, dédup comprise : on supprimait des instantanés réels pour écrire une courbe bâtie sur 7 % des mouvements | un compte carte sans courbe rétrospective — réversible, contrairement à la destruction |

## Pièges vérifiés sur le terrain — ne pas les rouvrir

1. **`import_bank_account_history(overwrite=True)` (`services/bank.py`) supprime TOUT l'historique du
   compte**, pas la fenêtre. `replace_history_window()` existe pour ça. Le test bloquant a été **prouvé
   non-vide par mutation** : substituer `overwrite=True` rend 4 tests rouges.
2. L'`uid` de compte expire avec la session ; **`identification_hash` est la clé durable**.
3. **Une page de pagination vide n'est pas la fin** — on s'arrête sur l'absence de `continuation_key`,
   qui voyage **avec** les paramètres d'origine.
4. **Les descriptions d'énumérations du fichier OpenAPI sont désalignées de leurs valeurs.** Mapper par
   nom, jamais par position.
5. **93 % des transactions carte existent aussi sur le compte courant, sans référence commune.**
   La dédup croisée est scopée **utilisateur**, pas compte.
6. `strategy=longest` **seul** s'auto-limite à 2 ans : le premier import exige `longest` **et** un
   `date_from` très ancien.
7. **Ne jamais appeler `DELETE /sessions/{id}` pour de vrai.**

## Points ouverts à traiter avant de clore

- ~~**⚠️ `cash_account_type == "CARD"` n'est pas vérifié contre la vraie banque.**~~ **Levé le
  2026-08-26.** Le marqueur était déjà dans les données réelles : `export-boursorama-2022-2026.json`,
  `.accounts[].info.cash_account_type` vaut **`CACC`** sur le compte courant (« CAV - BOURSOBANK ») et
  **`CARD`** sur le compte carte (« Carte Visa Ultim », `details: "immediat_debit"`). Le contrat le
  confirme : `cash_account_type` est dans le bloc `required` de `AccountResource`
  (`enablebanking-api.yaml:1866`), et `AuthorizeSessionResponse.accounts` est un `AccountResource[]` —
  `POST /sessions` ne peut donc pas l'omettre. R12, R18 et R19 reposent sur un champ réel et
  correctement valué. *Reste non prouvé* : qu'une **autre** banque étiquette pareil sa carte —
  `card_marker_missing` garde donc son utilité.
  Au passage, `currency` vaut bien `"XXX"` sur les deux comptes, ce que le code suppose déjà.
- ~~**⚠️ Le sélecteur de banque** : l'hypothèse de préfixe des caisses régionales n'est pas
  confirmée.~~ **Levé le 2026-08-26** par `vendor-docs/spike/aspsps-FR-2026-08-26.json` (capture
  réelle, HTTP 200, 129 établissements) : **aucune entrée générique n'existe**. Ni « Crédit Agricole »,
  ni « Banque Populaire », ni « Caisse d'Épargne » seuls — les 68 caisses régionales sont listées
  comme entrées à part entière (« Crédit Agricole du Languedoc », « Banque Populaire du Nord », …).
  `BankLinkModal` ne déclenche son étape `region` que sur une égalité **exacte** avec un nom de
  `REGIONAL_NETWORKS`, qui ne peut donc jamais se produire : **l'étape régionale est du code mort**
  (`REGIONAL_NETWORKS`, `loadRegionalOptions`, `retryRegionalOptions`, `filteredRegionalOptions`,
  `selectRegional`, l'état `region`). À supprimer.
  *Dernière vérification à faire à la main* : ouvrir le widget et cliquer un Crédit Agricole, pour
  écarter le cas où il grouperait par réseau et émettrait le nom du groupe.
- **`locale="FR"` sur le sélecteur** reste non documenté pour ce composant (inoffensif s'il est ignoré).
- **La machine à états de `BankLinkModal` n'a aucun test** — le dépôt n'a ni `@vue/test-utils` ni
  environnement DOM. Elle a été validée par relecture exhaustive des branches, pas par exécution.
- **`Bank.vue:144-159`** (pré-existant) ouvre une confirmation depuis une modale déjà ouverte : même
  patron d'empilement Teleport qu'un défaut déjà corrigé ailleurs. Pour la revue finale.
- **Divergence export ↔ API**, mesurée : la même opération est `CHF 12.63 / DBIT / PDNG` via l'API et
  `EUR -12.63 / DBIT / OTHR` dans l'export. Devise, **signe** et statut diffèrent. La dédup entre les
  deux chemins ne tient que par `entry_reference`. **Crucial pour les Tasks 11 et 12.**
- **`TZ` du conteneur de production** : `last_synced_at` est la date civile locale du serveur. En UTC
  avec un utilisateur à Paris, une synchro entre 00 h et 02 h se lit périmée côté front — un appel
  gaspillé, jamais de double synchro. `TZ=Europe/Paris` l'élimine. Décision de déploiement.
- Environ **25 findings Minor** différés à la revue finale, listés dans le ledger local.

## Contraintes d'environnement

- Tests API : `uv run pytest` **exige `dangerouslyDisableSandbox: true`** (cache uv bloqué).
- Front : `node` absent du PATH → `export PATH="$(dirname $(head -1 $(which pnpm) | sed 's|^#!||')):$PATH"`,
  puis `pnpm type-check`. Éviter `pnpm build` (run-p).
- Tests sur **SQLite in-memory**, production **PostgreSQL**. `pg_insert` contourné par la fixture
  `sqlite_pg_insert` (`tests/services/test_bank.py`).
- **`PRAGMA foreign_keys` n'est jamais activé** : aucune contrainte de clé étrangère n'est réellement
  exercée par les tests. Un test qui prétend valider une FK ne valide rien.
- **Aucun appel réseau réel dans les tests** — le client est injecté et doublé.
- Le tag `sdd-rescue-task10-preFix` protège un commit web orphelin d'une reprise antérieure.
