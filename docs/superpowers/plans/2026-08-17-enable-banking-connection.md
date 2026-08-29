# Connexion bancaire Enable Banking — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** connecter un compte bancaire réel via Enable Banking pour que la courbe de solde soit exacte et vérifiée à partir de la connexion, et stocker les mouvements constatés dans une table dédiée.

**Architecture :** côté API, un module `services/banking/` indépendant du framework d'import porte le client Enable Banking (jeton RS256, pagination, taxonomie d'erreurs) ; cinq tables nouvelles stockent les identifiants BYO, les parcours d'autorisation, les consentements, le rattachement des comptes et les transactions, toutes chiffrées avec index aveugles ; la synchronisation est déclenchée par le front après rendu, plafonnée à une par jour côté serveur, et réécrit `AccountHistory` par un remplacement **borné à une fenêtre**. Côté web, un `SettingsBanking.vue` calqué sur `SettingsAI.vue` et le sélecteur de banque fourni par Enable Banking.

**Tech Stack :** FastAPI + SQLModel + Alembic + pytest (`capitalview-api`) ; Vue 3 `<script setup>` + Pinia + Tailwind v4 (`capitalview-web`).

**Spec :** `capitalview-api/docs/superpowers/specs/2026-08-17-enable-banking-connection-design.md`

## Global Constraints

- Deux repos git distincts, chacun sur la branche `feat/enable-banking-connection`. Commits séparés par repo.
- Commentaires de code **en anglais**, densité faible, uniquement là où le *pourquoi* n'est pas évident.
- Commits en conventional commits anglais, 2-3 lignes maximum, scope quand il est évident.
- Toute donnée utilisateur est chiffrée via `encrypt_data`/`decrypt_data` avec la `master_key`. Les recherches passent par `hash_index`. **Aucune date de transaction en clair.**
- Restent en clair, délibérément : `bank_sessions.status`, `bank_sessions.consent_valid_until`, `bank_account_links.anchor_date`, `bank_account_links.last_synced_at`. Ce sont des métadonnées opérationnelles, et c'est la seule voie pour qu'un job sans Master Key notifie une expiration.
- Tests API : `uv run pytest` — **nécessite `dangerouslyDisableSandbox: true`** (le cache uv est bloqué par le sandbox).
- Build web : `node` n'est pas sur le PATH. Préfixer par `export PATH="$(dirname $(head -1 $(which pnpm) | sed 's|^#!||')):$PATH"`, puis `pnpm type-check`. Éviter `pnpm build` (run-p).
- **Aucun appel réseau réel dans les tests.** Le client Enable Banking est injecté et remplacé par un double en test.
- **Ne jamais appeler `DELETE /sessions/{id}` en développement** sans intention explicite : la fermeture du consentement côté banque exige une nouvelle authentification forte pour la rétablir.
- La clé privée n'est **jamais** renvoyée au client : l'API expose un booléen de présence, comme `AIProviderConfig.has_key`.
- Documentation de référence locale : `vendor-docs/enablebanking-api.yaml` (contrat OpenAPI officiel) et `vendor-docs/enablebanking-docs-utile.md`. **Les descriptions d'énumérations du fichier OpenAPI sont désalignées de leurs valeurs — mapper par nom, jamais par position.**

## Références locales

Tout est dans `vendor-docs/`, à la racine du dépôt. **Aucun accès réseau n'est nécessaire pour implémenter ce plan.**

| Ressource | Chemin | Ce qu'on y trouve |
| --- | --- | --- |
| **Contrat OpenAPI officiel** | `vendor-docs/enablebanking-api.yaml` | Fait autorité : paramètres, énumérations, sémantique exacte de chaque champ |
| **Implémentation de référence** | `vendor-docs/enablebanking-api-samples/python_example/account_information.py` | Le parcours complet en 140 lignes : jeton, autorisation, session, soldes, pagination |
| **Collection Postman** | `vendor-docs/enablebanking-api-samples/postman_example/` | Surface d'API complète |
| **Doc curée** | `vendor-docs/enablebanking-docs-utile.md` | Prose absente du contrat |
| **Doc découpée par sujet** | `vendor-docs/eb-docs-split/*.md` | Même contenu, un fichier par page |
| **Rapport d'étude** | `vendor-docs/rapport-enable-banking-spike-2026-08-16.md` | Le pourquoi de chaque décision, les cas limites mesurés |
| **Outil de spike** | `vendor-docs/spike/eb_spike.py` | Client en ligne de commande validé contre Boursorama |
| **Jeux de données réels** | `vendor-docs/spike/` | Voir ci-dessous |

**Jeux de données disponibles pour les tests** — données bancaires réelles, hors dépôt git, à ne pas committer :

| Fichier | Contenu | Sert à tester |
| --- | --- | --- |
| `export-boursorama-2022-2026.json` | 2 776 + 1 464 transactions, oct. 2022 → août 2026, 2 soldes | Volume, **1 360 doublons croisés**, libellés authentiques. C'est ce qui alimente la banque fictive |
| `tx_courant.json` | 297 transactions, réponse API brute | **Opérations en attente**, `booking_date` absent |
| `tx_carte.json` | 202 transactions, réponse API brute | **Devise étrangère sans taux de change** |

Les deux derniers sont précieux **parce qu'ils contiennent ce que l'export ne contient pas** : sans eux, trois cas limites du §F de la spec ne sont couverts par aucune donnée réelle.

Correspondance tâche par tâche :

| Tâche | À lire avant |
| --- | --- |
| 1 | `services/settings.py:258` (`update_ai_provider`, le patron BYO à copier) |
| 2 | `eb-docs-split/quick-start.md` (jeton, séquence) · `python_example/account_information.py` l. 22-34 (signature) et l. 110-134 (**boucle de pagination correcte**) · `eb-docs-split/faq.md` (limites de débit, en-têtes utilisateur) · `enablebanking-api.yaml` (codes d'erreur) |
| 3 | `enablebanking-api.yaml`, schémas `SessionAccount` et `AccountResource` (**`uid` éphémère vs `identification_hash` durable**) |
| 4 | `eb-docs-split/quick-start.md` (paramètres de `/auth`, retour) · `eb-docs-split/linked-accounts.md` (lier ≠ autoriser) · `enablebanking-api.yaml`, champ `Access.valid_until` (**sémantique contre-intuitive**) |
| 5 | `enablebanking-api.yaml`, schéma `Transaction` (`entry_reference` unique mais **pas globalement**, `transaction_id` inutilisable) · `eb-docs-split/api-reference.md` |
| 6 | `services/bank.py:338` (`import_bank_account_history` — **lire le piège `overwrite`**) · `services/bank.py:140` (`_apply_pending_cashflows`) · `eb-docs-split/faq.md` (stratégies, fenêtre d'historique) |
| 7 | `enablebanking-api.yaml`, énumération `SessionStatus` (**huit valeurs, descriptions désalignées**) · `eb-docs-split/faq.md` (causes d'expiration prématurée) · `models/notification.py` |
| 9-10 | `eb-docs-split/widgets.md` (sélecteur de banque) · `eb-docs-split/markets-fr.md` (caisses régionales, bascule mobile) · `pages/settings/SettingsAI.vue` |
| 11 | `eb-docs-split/sandbox.md` (banque fictive, lots de dix, filtrage non supporté) |

---

## Task 1 : identifiants Enable Banking par utilisateur

**Files:**
- Create: `capitalview-api/models/banking.py`
- Create: `capitalview-api/alembic/versions/<rev>_add_user_bank_connections.py`
- Create: `capitalview-api/dtos/banking.py`
- Create: `capitalview-api/services/banking/__init__.py`
- Create: `capitalview-api/services/banking/credentials.py`
- Test: `capitalview-api/tests/services/test_banking_credentials.py`

**Interfaces:**
- Consomme : `encrypt_data`, `decrypt_data`, `hash_index` de `services.encryption`.
- Produit : `UserBankConnection`, `BankConnectionUpdate`, `BankConnectionStatus(has_credentials: bool, application_id: str | None)`, `upsert_connection()`, `get_connection()`, `delete_connection()`.
- Les Tasks 2 et 5 consomment `get_connection()`.

- [ ] **Step 1 : écrire les tests qui échouent** — création avec identifiant et clé, relecture, remplacement, suppression ; la clé stockée n'est jamais égale au texte clair ; `has_credentials` est faux après suppression ; deux utilisateurs différents produisent des `user_uuid_bidx` différents pour la même Master Key logique.
- [ ] **Step 2 : lancer les tests et vérifier qu'ils échouent**
- [ ] **Step 3 : créer le modèle** `UserBankConnection` dans `models/banking.py` — table `user_bank_connections`, `user_uuid_bidx` unique indexé, `application_id_enc`, `private_key_enc`, horodatages. L'enregistrer dans `models/__init__.py`.
- [ ] **Step 4 : écrire la migration Alembic**, dans le style des migrations existantes.
- [ ] **Step 5 : DTOs et service** — `upsert_connection` suit exactement la logique de `update_ai_provider` (`services/settings.py:258`) : champ absent = inchangé, chaîne vide = suppression. La réponse ne contient **jamais** la clé.
- [ ] **Step 6 : lancer les tests et vérifier qu'ils passent**
- [ ] **Step 7 : commit**

---

## Task 2 : client Enable Banking — jeton et appels de lecture

**Files:**
- Create: `capitalview-api/services/banking/client.py`
- Create: `capitalview-api/services/banking/errors.py`
- Test: `capitalview-api/tests/services/test_banking_client.py`

**Interfaces:**
- Produit : `EnableBankingClient(application_id, private_key, psu_context=None)` avec `get_application()`, `list_aspsps(country)`, `start_authorization(...)`, `create_session(code)`, `get_session(session_id)`, `get_balances(uid)`, `iter_transactions(uid, date_from=None, strategy="default")`.
- Produit : `BankingApiError(code, message, detail)` et les familles de `errors.py`.
- Les Tasks 4, 5 et 6 consomment ce client.

- [ ] **Step 1 : écrire les tests qui échouent** — le jeton porte `kid`, `iss`, `aud` et une expiration ≤ 86 400 s ; `iter_transactions` **ne s'arrête pas sur une page vide** tant qu'une clé de continuation est présente ; la clé de continuation est renvoyée **avec** les paramètres d'origine ; le nombre de pages est borné ; une erreur métier est levée avec son code, pas avec le statut HTTP ; les en-têtes de contexte utilisateur sont envoyés **tous ou aucun**.
- [ ] **Step 2 : lancer les tests et vérifier qu'ils échouent**
- [ ] **Step 3 : implémenter le client** — `httpx`, base `https://api.enablebanking.com`. La signature du jeton reprend celle validée par le spike (`scratchpad/eb_spike.py`) et l'exemple officiel `vendor-docs/enablebanking-api-samples/python_example/account_information.py`.
- [ ] **Step 4 : implémenter la taxonomie d'erreurs** de la spec §B5, en mappant le champ `error` de la réponse, **jamais** le statut HTTP.
- [ ] **Step 5 : lancer les tests et vérifier qu'ils passent**
- [ ] **Step 6 : commit**

---

## Task 3 : tables de session, de rattachement et d'autorisation

**Files:**
- Modify: `capitalview-api/models/banking.py`
- Create: `capitalview-api/alembic/versions/<rev>_add_bank_sessions_links_authorizations.py`
- Test: `capitalview-api/tests/models/test_banking_models.py`

**Interfaces:**
- Produit : `BankAuthorization`, `BankSession`, `BankAccountLink` tels que décrits en spec §A2–A4.
- La Task 4 consomme `BankAuthorization` et `BankSession` ; les Tasks 5 et 6, `BankAccountLink`.

- [ ] **Step 1 : écrire les tests qui échouent** — contraintes d'unicité, colonnes en clair conformes à la liste des Global Constraints, `bank_account_uuid_bidx` unique.
- [ ] **Step 2 : lancer les tests et vérifier qu'ils échouent**
- [ ] **Step 3 : créer les trois modèles.** Rappel : `account_uid_enc` est **jetable** (il expire avec la session) ; `identification_hash_bidx` est la **clé de rattachement durable**.
- [ ] **Step 4 : écrire la migration Alembic**
- [ ] **Step 5 : lancer les tests et vérifier qu'ils passent**
- [ ] **Step 6 : commit**

---

## Task 4 : parcours de liaison — ouverture, retour, contrôle de configuration

**Files:**
- Create: `capitalview-api/services/banking/linking.py`
- Create: `capitalview-api/routes/banking.py`
- Modify: `capitalview-api/main.py`
- Test: `capitalview-api/tests/routes/test_banking_linking.py`

**Interfaces:**
- Produit : `GET /banking/status`, `PUT /banking/credentials`, `GET /banking/check`, `GET /banking/aspsps`, `POST /banking/authorize`, `GET /banking/callback`, `DELETE /banking/sessions/{uuid}`.
- La Task 5 consomme les `BankAccountLink` créés ici.

- [ ] **Step 1 : écrire les tests qui échouent** — `GET /banking/check` signale une URL de callback absente des URL déclarées ; `POST /banking/authorize` demande **`valid_until` au maximum autorisé** par la banque ; le retour valide `state` via son index aveugle ; un `state` inconnu ou rejoué est rejeté ; un retour portant `error=access_denied` produit un message de refus et non une erreur technique ; un code rejoué est idempotent.
- [ ] **Step 2 : lancer les tests et vérifier qu'ils échouent**
- [ ] **Step 3 : implémenter le contrôle de configuration** via `GET /application` — clé valide, application active, URL de callback déclarée. Diagnostic rendu **avant** que l'utilisateur ne parte s'authentifier.
- [ ] **Step 4 : implémenter l'ouverture** — `state` aléatoire persisté en index aveugle, `psu_type="personal"`, `valid_until` = maximum lu dans le catalogue.
- [ ] **Step 5 : implémenter le retour** — trois issues (succès, refus, échec) ; persister **l'intégralité** de la réponse d'ouverture de session, certaines informations n'étant fournies qu'une fois ; créer un `BankAccountLink` par compte, indexé sur `identification_hash`.
- [ ] **Step 6 : implémenter le rattachement à un compte CapitalView existant** — soit un compte existant choisi par l'utilisateur, soit création. Sur une **reconnexion**, retrouver le lien par `identification_hash_bidx` et le mettre à jour au lieu d'en créer un nouveau.
- [ ] **Step 7 : page de retour sans session** — expliquer de terminer dans l'onglet connecté. **Ne pas** implémenter le report du code chiffré (spec §C3).
- [ ] **Step 8 : lancer les tests et vérifier qu'ils passent**
- [ ] **Step 9 : commit**

---

## Task 5 : table des transactions et déduplication

**Files:**
- Modify: `capitalview-api/models/banking.py`
- Create: `capitalview-api/alembic/versions/<rev>_add_bank_transactions.py`
- Create: `capitalview-api/services/banking/transactions.py`
- Test: `capitalview-api/tests/services/test_banking_transactions.py`

**Interfaces:**
- Produit : `BankTransaction`, `normalize_transaction()`, `store_transactions()` renvoyant `(inserted, updated, skipped)`.
- La Task 6 consomme `store_transactions()`.

- [ ] **Step 1 : écrire les tests qui échouent** — couvrir **tous** les cas de la spec §H : référence absente ; même référence sur deux comptes différents (ne doit **pas** dédupliquer) ; même opération sur compte carte et compte courant avec références différentes (doit dédupliquer) ; `booking_date` absent ; devise étrangère ; flux dans le désordre ; opération passant d'« en attente » à « comptabilisée » avec changement de référence (mise à jour, pas doublon) ; statuts annulé et rejeté invalidant une ligne existante.
- [ ] **Step 2 : lancer les tests et vérifier qu'ils échouent**
- [ ] **Step 3 : créer le modèle et la migration** — index unique composite `(account_id_bidx, entry_ref_bidx)`, index sur `period_bidx` et `dedup_bidx`.
- [ ] **Step 4 : implémenter la normalisation** — date retenue avec repli `booking_date` → `transaction_date` → `value_date` ; libellé multi-lignes concaténé selon une règle stable ; montants en `Decimal` ; devise étrangère marquée non convertie ; **ne jamais utiliser `transaction_id`**.
- [ ] **Step 5 : implémenter la déduplication à trois niveaux** de la spec §E. La détection croisée est **scopée à l'utilisateur**, pas au compte.
- [ ] **Step 6 : lancer les tests et vérifier qu'ils passent**
- [ ] **Step 7 : commit**

---

## Task 6 : synchronisation, ancres et réconciliation

**Files:**
- Create: `capitalview-api/services/banking/sync.py`
- Modify: `capitalview-api/services/bank.py`
- Modify: `capitalview-api/routes/banking.py`
- Test: `capitalview-api/tests/services/test_banking_sync.py`

**Interfaces:**
- Produit : `POST /banking/sync`, `sync_account_link()`, `replace_history_window()`.
- Consomme : `store_transactions()` (Task 5), `EnableBankingClient` (Task 2).

- [ ] **Step 1 : écrire les tests qui échouent** — un second appel le même jour est sans effet ; le solde retenu est le **comptable**, jamais le temps réel ; la réconciliation qui tombe juste ne stocke aucun écart ; celle qui échoue stocke un écart daté ; les opérations en attente sont exclues du calcul ; les instantanés ne vont **jamais au-delà d'hier**.
- [ ] **Step 2 : écrire le test qui protège du piège destructeur** — sur un compte possédant plusieurs années d'historique manuel, l'amorçage ne remplace **que** la fenêtre traitée et **rien** d'antérieur. Ce test est obligatoire et bloquant.
- [ ] **Step 3 : lancer les tests et vérifier qu'ils échouent**
- [ ] **Step 4 : implémenter `replace_history_window()`** — remplacement borné à un intervalle de dates. **Ne pas réutiliser `overwrite=True` d'`import_bank_account_history`, qui supprime tout l'historique du compte** (spec §D4).
- [ ] **Step 5 : implémenter la séquence de synchro** (spec §D2) — `strategy=longest` au premier passage, `default` ensuite depuis `anchor_date` ; en-têtes de contexte utilisateur renseignés depuis la requête réelle ; plafond quotidien **revérifié côté serveur**.
- [ ] **Step 6 : implémenter la réconciliation** (spec §D3) et le calcul de la courbe entre ancres.
- [ ] **Step 7 : neutraliser la projection sur les comptes liés** dans `_apply_pending_cashflows` (`services/bank.py:140`) — exclure le compte **tout en avançant `balance_updated_at`**. Ajouter un test dédié.
- [ ] **Step 8 : lancer la suite bancaire complète**
- [ ] **Step 9 : commit**

---

## Task 7 : cycle de vie des consentements

**Files:**
- Create: `capitalview-api/services/banking/health.py`
- Modify: `capitalview-api/models/notification.py`
- Modify: `capitalview-api/main.py`
- Test: `capitalview-api/tests/services/test_banking_health.py`

**Interfaces:**
- Produit : `check_session_health()`, type de notification `bank_consent_expiring`.

- [ ] **Step 1 : écrire les tests qui échouent** — les huit états de session sont distingués ; une session expirée n'entraîne **aucune** perte de rattachement ; le job produit une notification avant l'échéance **sans Master Key** ; une expiration détectée en cours d'appel est traitée comme une réponse possible de n'importe quel appel, pas comme un état vérifiable en amont.
- [ ] **Step 2 : lancer les tests et vérifier qu'ils échouent**
- [ ] **Step 3 : implémenter le contrôle de santé** — mapper les états **par nom** ; distinguer expirée, révoquée, annulée et invalide, qui appellent des messages différents.
- [ ] **Step 4 : brancher le job sur l'APScheduler existant** (`main.py`) — il ne lit que `consent_valid_until`, en clair, et écrit une `Notification`, en clair.
- [ ] **Step 5 : lancer les tests et vérifier qu'ils passent**
- [ ] **Step 6 : commit**

---

## Task 8 : suppression de compte et export

**Files:**
- Modify: `capitalview-api/services/account_data.py`
- Test: `capitalview-api/tests/services/test_account_data.py`

- [ ] **Step 1 : écrire les tests qui échouent** — la suppression d'un compte utilisateur purge les cinq tables nouvelles ; l'export inclut les transactions bancaires et **exclut** la clé privée.
- [ ] **Step 2 : lancer les tests et vérifier qu'ils échouent**
- [ ] **Step 3 : implémenter**, en fermant proprement les sessions Enable Banking côté banque quand c'est possible.
- [ ] **Step 4 : lancer les tests et vérifier qu'ils passent**
- [ ] **Step 5 : commit**

---

## Task 9 : réglages web — identifiants et diagnostic

**Files:**
- Create: `capitalview-web/src/pages/settings/SettingsBanking.vue`
- Modify: `capitalview-web/src/types/index.ts`
- Modify: `capitalview-web/src/stores/settings.ts`
- Modify: `capitalview-web/src/router/index.ts`

- [ ] **Step 1 : implémenter l'écran**, calqué sur `SettingsAI.vue`, avec **dépôt de fichier** pour la clé privée et **affichage copiable** de l'URL de callback à déclarer.
- [ ] **Step 2 : brancher le diagnostic** `GET /banking/check` et afficher un message par cause : clé invalide, application inactive, URL de callback absente.
- [ ] **Step 3 : documenter le parcours dans l'écran** — les sept étapes, en avertissant que **lier un compte au portail n'autorise pas l'accès** et qu'une seconde authentification bancaire suivra.
- [ ] **Step 4 : `pnpm type-check`**
- [ ] **Step 5 : commit**

---

## Task 10 : parcours de liaison web et page Banque

**Files:**
- Create: `capitalview-web/src/components/banking/BankLinkModal.vue`
- Modify: `capitalview-web/src/pages/Bank.vue`
- Modify: `capitalview-web/src/stores/bank.ts`

- [ ] **Step 1 : intégrer le sélecteur de banque** fourni par Enable Banking — pays `FR`, type `personal`, service `AIS`, langue française, intégrations en bêta masquées. **Aucune déclaration d'origine n'est nécessaire pour ce composant** ; ne pas utiliser les deux autres.
- [ ] **Step 2 : gérer l'étape de caisse régionale** pour Crédit Agricole, Banque Populaire et Caisse d'Épargne.
- [ ] **Step 3 : afficher l'état des connexions** sur la page Banque — dernier sync, consentement expirant, bouton de synchro manuelle, et **signalement visible d'un écart de réconciliation**.
- [ ] **Step 4 : déclencher la synchro automatique après le rendu** quand `last_synced_at` est antérieur à aujourd'hui. **Ne jamais bloquer le rendu de la page sur cet appel.**
- [ ] **Step 5 : `pnpm type-check`**
- [ ] **Step 6 : commit**

---

## Task 11 : import d'un export Enable Banking (rattrapage d'historique)

**Files:**
- Create: `capitalview-api/services/banking/export_import.py`
- Modify: `capitalview-api/routes/banking.py`
- Test: `capitalview-api/tests/services/test_banking_export_import.py`

**Interfaces:**
- Produit : `POST /banking/import-export`, `import_enablebanking_export(session, payload, master_key)`.
- Consomme : `normalize_transaction()` et `store_transactions()` de la Task 5.

**Pourquoi cette tâche.** L'API ne rend l'historique complet que dans l'heure suivant une autorisation ; passé ce délai, beaucoup de banques retombent à 90 jours — mesuré chez Boursorama. L'interface de démonstration d'Enable Banking permet en revanche d'exporter en JSON l'intégralité de l'historique. Cette tâche offre un rattrapage **définitif et indépendant de la fenêtre d'une heure**.

**Le coût est minime** : l'export a exactement la même forme que les réponses de l'API. Structure `{"accounts": [{"info", "transactions", "balances", "raw_data"}]}`, où chaque transaction est un objet identique à ceux renvoyés par `GET /accounts/{uid}/transactions`. **Aucune normalisation spécifique n'est à écrire** : on réutilise celle de la Task 5.

- [ ] **Step 1 : écrire les tests qui échouent** — le rattachement se fait par `identification_hash`, pas par nom de compte ; la déduplication avec les transactions déjà récupérées par l'API fonctionne (mêmes références, aucun doublon créé) ; un export portant sur un compte non lié est rejeté proprement ; la courbe de soldes est recalculée après import.
- [ ] **Step 2 : lancer les tests et vérifier qu'ils échouent**
- [ ] **Step 3 : implémenter l'ingestion**, en réutilisant `normalize_transaction()` et `store_transactions()`. Les soldes de l'export alimentent une ancre à leur `reference_date`.
- [ ] **Step 4 : vérifier sur le jeu réel** `vendor-docs/spike/export-boursorama-2022-2026.json` — 2 776 + 1 464 transactions, et **1 360 doublons croisés** à écarter.
- [ ] **Step 5 : marquer la portion importée comme estimée** dans la courbe : un export ne fournit qu'une ancre, la période antérieure n'est donc pas réconciliée (spec §4.4).
- [ ] **Step 6 : lancer les tests et vérifier qu'ils passent**
- [ ] **Step 7 : commit**

---

## Task 12 : validation de bout en bout contre la banque fictive

**Files:**
- Create: `capitalview-api/tests/integration/test_banking_e2e.py`

**Prérequis déjà remplis :** la banque fictive est configurée et chargée avec les données réelles exportées des deux comptes Boursorama — 2 776 transactions depuis octobre 2022 sur le compte courant, 1 464 depuis mai 2024 sur le compte carte, les deux soldes, et les mêmes empreintes d'identification qu'en production.

**Le parcours complet a déjà été validé manuellement le 17/08 contre la banque fictive** : autorisation, session, pagination sur 28 pages, 2 776 transactions de 2022 à 2026. L'application bac à sable est `82452a6c-0dae-4331-b029-9be7a2e468ff`, active, avec les deux URL de redirection. La banque fictive préserve les mêmes empreintes d'identification qu'en production — les tests exercent donc la vraie clé de rattachement.

- [ ] **Step 1 : écrire le parcours complet** contre la banque fictive — liaison, synchro, seconde synchro sans effet, reconnexion après expiration.
- [ ] **Step 2 : vérifier la recette du premier import** — `strategy=longest` **avec** un `date_from` très ancien doit rendre 2 776 transactions ; `longest` **seul** n'en rend que 1 987, silencieusement. C'est le test qui protège de la perte de deux ans d'historique (spec §B4).
- [ ] **Step 3 : vérifier la déduplication croisée à l'échelle réelle** — **1 360 des 1 464 opérations du compte carte, soit 93 %, existent aussi sur le compte courant, sans aucune référence commune**. C'est le test qui compte le plus : sans lui, l'immense majorité des dépenses carte serait comptée deux fois.
- [ ] **Step 4 : ne dépendre d'aucune taille de lot** — la documentation annonce dix transactions par page, la mesure en donne cent.
- [ ] **Step 5 : ne pas tenter de couvrir ici** les opérations en attente, les devises étrangères, les dates de comptabilisation absentes ni la page vide en début de pagination — **la banque fictive ne les reproduit pas** (spec §H). Ces cas restent couverts par les doubles des Tasks 2 et 5.
- [ ] **Step 4 : lancer la suite complète de l'API**
- [ ] **Step 5 : commit**
