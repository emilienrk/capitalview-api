# Catégories d'opérations et cashflow réel — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** ranger chaque opération bancaire (type, nature, catégorie personnalisée), à la main ou assisté par IA, puis en tirer un **cashflow réel** par année et par mois terminé, à côté du cashflow **visé** qui existe déjà.

**Architecture :** côté API, trois nouveautés sous `services/banking/` : `operation_types.py` (type lu dans le libellé), `categories.py` (catégories, règles, visibilité), `categorize.py` (résolution pure : dérogation → règle → rien, puis nature). La résolution s'applique **à la lecture**, dans le pipeline de `flows.py`, exactement comme l'appariement des virements : aucune catégorie n'est recopiée sur les lignes, sauf la dérogation manuelle d'une opération. `real_cashflow.py` agrège par nature et par catégorie. L'IA est un agent à sortie JSON contrainte, appelé par lots depuis le front. Côté web : une puce catégorie sur chaque opération, un onglet **Catégories** dans Banque (file « à ranger », gestion, bouton IA), et une vue **Réel** sur la page Flux.

**Tech Stack :** FastAPI + SQLModel + Alembic + pytest (`capitalview-api`) ; Vue 3 `<script setup>` + Pinia + Tailwind v4 + vitest (`capitalview-web`).

---

## Ce qui a été décidé (brainstorming du 2026-09-15)

- **Cashflow visé** = le module Cashflow actuel, inchangé. **Cashflow réel** = calculé depuis les opérations bancaires.
- **Type d'opération** (carte, virement, prélèvement, retrait, intérêts, inconnu) : stocké en base, sert au filtre.
- **Nature** : dépense, revenu, épargne, investissement, interne, neutralisée. Seules dépense et revenu comptent comme tels ; épargne et investissement sont totalisés à part.
- **Catégories personnalisées**, sans liste imposée. Une seule table, chaque catégorie retient son **origine** (`cashflow`, `bank`, `ai`) et porte une **nature**.
- **Visibilité selon l'IA de catégorisation** (rien n'est déplacé quand on la bascule, seule la proposition change) :
  - IA **activée** : Banque et Réel proposent les catégories `bank` + `ai` ; le visé ne propose que ses propres catégories.
  - IA **désactivée** : tout est mélangé, une catégorie créée n'importe où est disponible partout, y compris les catégories textuelles déjà saisies dans les cashflows.
- **Rangement par règle** : une règle = un ensemble de mots qui doivent **tous** être présents dans le libellé (« carrefour » range `CARTE 21/06/26 CARREFOUR ANNECY CB*0837` comme `Carrefour`). Corriger une opération propose d'appliquer au groupe. Une opération peut aussi être rangée seule (dérogation).
- **IA légère** : un interrupteur avec un court avertissement, et une action « Ranger avec l'IA » qui traite par lots les groupes sans règle, relançable à la main. Elle crée des règles marquées IA, que l'utilisateur écrase quand il veut. Sans IA, tout fonctionne sauf les suggestions.
- **Réel** : vue **année par défaut** (totaux, moyenne et médiane mensuelles), vue **mois** limitée aux **mois terminés**, navigable vers le passé.
- **Mis de côté** : la comparaison visé ↔ réel existante (`CashflowComparisonCard`) est **masquée**, pas supprimée. Les séries récurrentes, leurs variations et une page abonnements viendront après.
- **Aucun cas particulier** codé pour une personne ou une contrepartie donnée : le moteur reste générique.

## Mesures qui fondent le plan (données réelles, 2026-09-15)

4 257 opérations, 4 comptes (Bourso courant, Revolut, Livret A, LDDS), mai 2022 → septembre 2026. Libellé présent sur 100 % des lignes, champs structurés vides (déjà établi).

- **Règles** : en simulant « une règle par groupe non couvert, mots les plus rares du libellé », il faut **~96 règles pour 80 % du volume des sorties** (983 signatures) et **~11 règles pour 80 % des entrées** (129 signatures).
- **Une clé « deux premiers mots utiles » est rejetée** : selon le seuil de mots fréquents, tous les prélèvements tombaient sous `prlv sepa`. D'où les règles par ensemble de mots, et non une clé figée.
- **Deux pièges vus dans la simulation** : une règle **vide** capte tout (716 signatures), et un libellé fait uniquement de mots fréquents (`carte cb`) produit une règle qui capte 620 signatures. Les deux doivent être impossibles (tâche A4).
- **Salaire** : le libellé embarque des références purement alphabétiques (`SALAIRE DE 2026-07 402147-GJPBAZZ…`), donc chaque salaire a sa propre signature. Une règle `vilmorin` les couvre tous.
- **Type d'opération** : un lexique minimal reconnaît 1 978 cartes, 115 prélèvements et 720 virements sur Bourso, mais **714 opérations Revolut restent inconnues** : ce sont des achats carte sans aucun préfixe (`Carrefour`, `Crous`).
- **Virements vers un compte d'épargne de l'app** : 270 opérations appariées `savings` sont aujourd'hui simplement exclues. Ce sont pourtant de l'épargne : elles doivent alimenter « mis de côté ».
- **~27 k€ de virements à son propre nom non appariés** (vers des comptes absents de l'app) : le moteur ne les devine pas, une règle catégorie de nature investissement ou épargne les range.

## Décision prise — type d'opération (2026-09-15)

`flows.py` et `REPRISE-flux-observes.md` posent un principe : **ne jamais interpréter un libellé avec du vocabulaire bancaire**. Les règles de catégorie le respectent (ce sont des mots choisis par l'utilisateur ou l'IA, comme les `tokens` de `transfer_decisions`). Le type d'opération, lui, exige un lexique : c'est **l'exception assumée**, bornée ainsi :

- Lexique court FR/EN isolé dans `operation_types.py`. Il ne devine **que quand c'est sans ambiguïté** et répond `UNKNOWN` sinon — pas `OTHER`, qui laisserait croire que l'opération a été identifiée.
- Le type ne sert **qu'à l'affichage et au filtre** : aucun total, aucun appariement, aucune nature ne le lit. Un format de banque raté donne un filtre incomplet, jamais un chiffre faux.
- Recalculé à la lecture pour les lignes sans type : améliorer le lexique corrige l'historique sans migration.
- Rejeté : la saisie manuelle seule, qui revient en pratique à n'avoir aucune donnée.

## Global Constraints

- Deux repos git, chacun sur la branche `feat/operation-categories` (créée depuis `main`). Commits séparés par repo.
- Commentaires **en anglais**, peu nombreux, uniquement le *pourquoi*. Densité alignée sur les fichiers existants.
- Conventional commits anglais, **3 lignes max**, aucune mention d'outil ou d'assistant.
- Toute donnée utilisateur chiffrée (`encrypt_data`/`decrypt_data`), recherches par `hash_index`. **Aucune date, aucun nom de catégorie, aucun mot de règle en clair.** Les tables sans colonne utilisateur (comme `bank_transactions`) ne doivent pas en gagner une.
- La résolution des catégories est **à la lecture**. Seule la dérogation manuelle d'une opération est écrite sur la ligne.
- Tests API : `uv run pytest -q` — **nécessite `dangerouslyDisableSandbox: true`**. Référence avant travaux : relever le nombre de tests en début de branche.
- Web : `node` n'est pas sur le PATH. `export PATH="$(dirname $(head -1 $(which pnpm) | sed 's|^#!||')):$PATH"`, puis `pnpm type-check` et `pnpm test`. Éviter `pnpm build`.
- **Mutation** : chaque test neuf est prouvé non vide en cassant volontairement le code qu'il couvre. Convention du dépôt.
- **Aucun appel réseau réel** : l'agent IA est testé avec un fournisseur factice injecté.
- Rejeu réel : la route `/dev/banking/study` de `routes/dev_debug.py` (exclu de git, à monter à la main dans `main.py` puis démonter) exporte les opérations dans `.pytest_cache/dev_study.json`. **Supprimer le dump après usage.**

---

# Partie A — Ranger les opérations

## Tâche A1 — Schéma

**Fichiers :** `models/banking.py`, `models/user.py`, `models/__init__.py`, nouvelle migration Alembic (tête actuelle : `28fdf21d9e17`), `services/account_data.py`.

- [ ] Table `bank_categories` : `uuid`, `user_uuid_bidx` (index), `name_enc`, `name_bidx` (nom normalisé casse/accents, unicité par utilisateur), `nature_enc` (`EXPENSE|INCOME|SAVING|INVESTMENT`), `origin_enc` (`cashflow|bank|ai`), `created_at`, `updated_at`. Contrainte unique `(user_uuid_bidx, name_bidx)`.
- [ ] Table `bank_category_rules` : `uuid`, `user_uuid_bidx` (index), `tokens_enc` (JSON, liste triée de mots), `tokens_bidx` (unicité par utilisateur de l'ensemble de mots), `category_ref_enc` (uuid de catégorie chiffré — pas de clé étrangère en clair, même raison que `BankTransferDecision`), `source_enc` (`user|ai`), `created_at` (fixé par le service à la microseconde, pour départager). Contrainte unique `(user_uuid_bidx, tokens_bidx)`.
- [ ] `bank_transactions` : `operation_type_enc` (nullable, rempli à l'écriture, rattrapé à la lecture) et `category_ref_enc` (nullable, dérogation manuelle ; la valeur spéciale chiffrée `"none"` signifie « explicitement sans catégorie, ignorer les règles »).
- [ ] `user_settings.ai_categorization_enabled` : booléen en clair, `server_default false`, comme `ai_feature_enabled`. Exposé dans `dtos/settings.py` (update + response) et `services/settings.py`.
- [ ] `account_data.purge_account` efface les deux tables ; `export_account_data` exporte catégories et règles déchiffrées.
- [ ] Tests : migration montante et descendante ; purge et export couvrent les deux tables (mutation : retirer le `wipe` rougit le test).

## Tâche A2 — Type d'opération *(voir « Décision prise »)*

**Fichiers :** nouveau `services/banking/operation_types.py`, `services/banking/transactions.py` (`_apply`), `services/banking/flows.py` (rattrapage dans `transfer_patterns`), `dtos/banking.py`.

- [ ] Énum `OperationType` : `CARD`, `TRANSFER`, `DIRECT_DEBIT`, `WITHDRAWAL`, `INTEREST`, `UNKNOWN`.
- [ ] `operation_type(label: str | None) -> OperationType`, fonction pure. Lexique court FR/EN ancré en début de libellé quand c'est possible. Docstring : l'exception au principe « pas de vocabulaire bancaire », et pourquoi elle ne touche aucun total.
- [ ] Écrit dans `_apply` à chaque stockage ; rattrapé pour les lignes `NULL` dans la passe qui rattrape déjà `label_signature_bidx`.
- [ ] Tests : formes Bourso réelles (`CARTE …`, `PRLV SEPA …`, `VIR INST …`, `RETRAIT DAB …`, `*INTER.BRUTS …`, `TDF EMIS VIA CB …`), formes Revolut (`To X`, `Virement de : X`, `Paiement envoyé par X`, `Retrait d'espèces à …`, `Carrefour` → `UNKNOWN`), `None` → `UNKNOWN`. Un mot-clé ambigu hors début de libellé → `UNKNOWN`. Rattrapage d'une ligne `NULL` prouvé par mutation.

## Tâche A3 — Catégories et visibilité

**Fichiers :** nouveau `services/banking/categories.py`, nouveaux DTO dans `dtos/banking.py`.

- [ ] CRUD : `create_category(name, nature, origin)`, `rename`, `set_nature`, `delete_category`. Nom unique par utilisateur, insensible à la casse et aux accents (conflit → erreur 409 côté route). Supprimer une catégorie supprime ses règles ; une dérogation qui la vise se lit comme « sans catégorie » (pas de parcours des lignes, qui n'ont pas d'index utilisateur).
- [ ] `available_categories(scope: Literal["bank", "planned"], ai_enabled: bool) -> list[AvailableCategory]` :
  - `bank` + IA activée → origines `bank` et `ai`.
  - `planned` + IA activée → catégories textuelles distinctes des `Cashflow` de l'utilisateur, plus les catégories d'origine `cashflow`.
  - IA désactivée, les deux portées → l'union des trois origines **et** des catégories textuelles des cashflows, sans doublon de nom.
- [ ] Une catégorie textuelle de cashflow choisie dans Banque est **matérialisée** à ce moment en ligne `bank_categories` d'origine `cashflow`. Sa nature est déduite du `flow_type` majoritaire des cashflows qui la portent (`INFLOW` → `INCOME`, sinon `EXPENSE`).
- [ ] `Cashflow.category` reste du texte : choisir une catégorie de banque dans le visé écrit simplement son nom.
- [ ] Tests : les quatre combinaisons portée × IA, l'unicité insensible à la casse, la matérialisation, la suppression en cascade des règles. Chacun prouvé par mutation.

## Tâche A4 — Règles et résolution

**Fichiers :** nouveau `services/banking/categorize.py`, `services/banking/transfer_patterns.py` (version dérivée), `services/banking/flows.py`.

- [ ] `Rule(uuid, tokens: frozenset[str], category_uuid, source, created_at)`. Les mots sont ceux de `transactions.label_words` (minuscules, chiffres et ponctuation retirés) : une règle et un libellé parlent la même langue.
- [ ] `resolve(words, override, rules, categories) -> Resolution(category | None, source: "manual" | "user_rule" | "ai_rule" | None, rule_uuid | None)` :
  1. dérogation de la ligne (`"none"` → aucune catégorie, règles ignorées) ;
  2. règles dont **tous** les mots sont dans le libellé ; la plus spécifique gagne (le plus de mots), puis `user` avant `ai`, puis la plus récente ;
  3. sinon aucune.
- [ ] Une règle **vide est refusée** à la création, et une règle dont tous les mots sont « trop fréquents » aussi (voir la ligne suivante). Ce sont les deux pièges mesurés.
- [ ] `propose_tokens(label, word_frequency) -> list[str]` : les mots du libellé classés du plus rare au plus fréquent **dans l'historique de l'utilisateur** (fréquence = nombre de signatures distinctes qui contiennent le mot, pas nombre d'opérations — sinon « carrefour » passe pour du bruit). Garder jusqu'à deux mots sous le seuil de fréquence ; si aucun, renvoyer tous les mots du libellé. Aucun vocabulaire : seulement des comptes.
- [ ] `word_frequency` et le seuil sont dérivés dans la passe de `flows.transfer_patterns` et stockés dans `TransferPatterns` (champ nouveau, `_VERSION` incrémentée) : même garantie de fraîcheur par empreinte, pas de nouvelle table.
- [ ] `nature_of(movement, leg, category, accounts) -> Nature` :
  - paire déduite `REVERSAL`/`REFUND` → `NEUTRALIZED` ;
  - autre paire déduite : si **exactement un** des deux comptes est d'épargne (`SAVINGS`, `LIVRET_A`, `LIVRET_DEVE`, `LEP`, `LDD`, `PEL`, `CEL`) → `SAVING` (le sens se lit sur le compte non-épargne : un débit met de côté, un crédit reprend) ; sinon `INTERNAL` ;
  - sinon la nature de la catégorie ;
  - sans catégorie : `EXPENSE` au débit, `INCOME` au crédit.
- [ ] Le chargement des règles et catégories se fait **une fois par requête**, jamais par ligne.
- [ ] Tests purs sur `resolve` (priorités, spécificité, dérogation `"none"`, règle orpheline d'une catégorie supprimée), `propose_tokens` (cas salaire à référence alphabétique, cas `CARTE … CARREFOUR ANNECY CB*…` → `carrefour` avant `annecy` si l'historique le dit, libellé tout fréquent), `nature_of` (courant → Livret A = épargne, Livret A → LDDS = interne, remboursement = neutralisé). Mutation sur chaque branche.
- [ ] Rejeu réel (hors git) : nombre de règles simulées pour 80 % du volume, qui doit rester du même ordre que la mesure (~96 sorties, ~11 entrées), et aucune règle ne couvrant plus de 5 % des signatures.

## Tâche A5 — API

**Fichiers :** `routes/banking.py`, `dtos/banking.py`, `services/banking/flows.py`, `services/banking/categories.py`.

- [ ] `BankTransactionItem` gagne `operation_type` (jamais null : `UNKNOWN` à défaut), `nature`, `category_id`, `category_name`, `category_source` (`manual|user_rule|ai_rule|null`), `rule_id`. `_item_builder` les remplit via `categorize.resolve` et `nature_of`.
- [ ] `GET /banking/categories` (avec nombre de règles), `POST /banking/categories`, `PATCH /banking/categories/{id}`, `DELETE /banking/categories/{id}`.
- [ ] `GET /banking/categories/available?scope=bank|planned` — lit `ai_categorization_enabled`.
- [ ] `PUT /banking/transactions/{id}/category` — corps `{category_id | null, apply_to_similar: bool, tokens: list[str] | null}` :
  - `apply_to_similar=false` → écrit la dérogation sur la ligne (`null` → `"none"`) ;
  - `apply_to_similar=true` → crée ou remplace la règle `user` sur `tokens` (par défaut `propose_tokens`) et efface la dérogation de cette ligne. Réponse : l'opération à jour et le nombre d'opérations que la règle range désormais.
- [ ] `GET /banking/category-rules` et `DELETE /banking/category-rules/{id}`.
- [ ] `GET /banking/uncategorized?limit=` — groupes par signature des opérations **ni appariées ni rangées**, triés par volume : libellé exemple, sens, nombre, total, dernier jour, mots proposés. Sert la file « à ranger » et l'IA.
- [ ] Les totaux existants de `/flows` et `/transactions` **ne changent pas** dans cette partie.
- [ ] Tests de routes : 404 sur catégorie ou opération d'un autre utilisateur, 409 sur nom en double, 400 sur règle vide ou trop générale, et le parcours « corriger une opération → toutes les similaires sont rangées ».

## Tâche A6 — IA de catégorisation

**Fichiers :** nouveau `services/ai/agents/categorize_agent.py`, `routes/banking.py`, `services/banking/categories.py`.

- [ ] Garde : `ai_feature_enabled` **et** `ai_categorization_enabled`, sinon 403. Fournisseur résolu par `AIProviderManager` sur la capacité `chat`.
- [ ] `POST /banking/categorize/ai` traite **les 100 groupes sans règle les plus lourds** (`/uncategorized`) et répond `{processed, rules_created, categories_created, remaining}`. Le front rappelle tant que `remaining > 0` : pas de tâche de fond, donc la Master Key ne quitte jamais la requête.
- [ ] Entrée du modèle : pour chaque groupe un identifiant court, un libellé exemple, le sens, le nombre d'occurrences, le montant médian ; et la liste des catégories existantes (nom, nature).
- [ ] Sortie contrainte par `output_config` JSON schema (patron d'`extract_tx_agent.py`) : par groupe `{group_id, category_name | null, nature, confidence}`.
- [ ] Garde-fous, tous testés :
  - un `group_id` absent de l'entrée est ignoré ;
  - `nature` hors énum → ignoré ;
  - `null` ou `confidence < 0.6` → le groupe reste à ranger ;
  - un nom proche d'une catégorie existante (même `name_bidx`) réutilise la catégorie au lieu d'en créer une ;
  - au plus 15 nouvelles catégories par appel.
- [ ] Chaque groupe accepté crée une règle `source=ai` sur `propose_tokens`, jamais par-dessus une règle `user` existante.
- [ ] Tests avec un fournisseur factice : réponses valides, hallucinées (id inconnu, nature inventée), vides ; `remaining` décroît ; une règle `user` n'est jamais écrasée.

## Tâche A7 — Web : opérations et onglet Catégories

**Fichiers :** `src/types/index.ts`, nouveau `src/stores/bankCategories.ts`, `src/components/bank/BankTransactionRow.vue`, nouveau `src/components/bank/BankCategoryPicker.vue`, `src/pages/BankTransactions.vue`, `src/components/bank/BankTabs.vue`, nouvelle page `src/pages/BankCategories.vue`, `src/router`.

- [ ] Types et store : catégories, catégories disponibles, règles, groupes à ranger, action de catégorisation, boucle IA avec progression. Invalide le cache `bank:flows:*` après chaque écriture.
- [ ] `BankTransactionRow` : puce catégorie (nom, ou « À ranger » discret), petit badge « IA » quand `category_source === 'ai_rule'`, type d'opération en texte discret. Clic sur la puce → `BankCategoryPicker`.
- [ ] `BankCategoryPicker` (modale) : recherche parmi `available?scope=bank`, création inline (nom + nature), case « Appliquer aux opérations similaires » cochée par défaut avec les mots proposés en puces désactivables, choix « Sans catégorie ». Après enregistrement, un message court : « 42 opérations rangées ».
- [ ] `BankTransactions.vue` : filtres par catégorie (dont « À ranger ») et par type, cumulés avec les filtres existants ; le total filtré existant suit.
- [ ] Onglet **Catégories** (`/bank/categories`) :
  - la file « À ranger » triée par volume, avec rangement rapide par groupe ;
  - la liste des catégories : renommer, changer la nature, supprimer (confirmation), nombre de règles, règles dépliables et supprimables ;
  - quand l'IA de catégorisation est active : bouton « Ranger avec l'IA » avec barre de progression et bilan. Sinon, un lien vers Réglages → IA.
- [ ] Tests vitest : le store (boucle IA qui s'arrête à `remaining = 0` et sur erreur), le picker (case cochée par défaut, mots désactivables envoyés), le filtre « À ranger ».

## Tâche A8 — Web : réglage IA et catégories du visé

**Fichiers :** `src/pages/settings/SettingsAI.vue`, `src/stores/settings.ts`, `src/pages/Cashflow.vue`.

- [ ] Dans Réglages → IA, sous l'interrupteur global : interrupteur « Catégoriser mes opérations bancaires », avec `BaseAlert` : *« Les libellés et montants de vos opérations sont envoyés à votre fournisseur d'IA via votre propre clé API. Désactivez pour rester entièrement local : les règles et le rangement manuel continuent de fonctionner. »*
- [ ] Formulaire du cashflow visé : `existingCategories` vient de `available?scope=planned` (repli sur le calcul local actuel si l'appel échoue).
- [ ] `pnpm type-check` et `pnpm test` verts.

---

# Partie B — Cashflow visé / réel

## Tâche B1 — API cashflow réel

**Fichiers :** nouveau `services/banking/real_cashflow.py`, `routes/banking.py`, `dtos/banking.py`.

- [ ] Réutilise le pipeline de `flows.py` (`_user_accounts`, `_pairing`, `_paired_movements`) puis `categorize` : **aucune seconde règle de calcul**.
- [ ] Mois pris en compte : **uniquement les mois terminés** (le mois courant et les suivants sont exclus). Opérations en attente exclues. Devise principale comme dans `_aggregate`, les autres rapportées à part.
- [ ] `GET /banking/real-cashflow?year=YYYY` → `RealCashflowYear` :
  - `years_available` (de la première opération à l'année courante) ;
  - `months` : par mois terminé, entrées, dépenses, épargne nette, investissement net, interne et neutralisé (montants informatifs) ;
  - totaux de l'année par nature ;
  - **moyenne et médiane mensuelles** par nature, calculées sur les mois qui portent des données (même raison que `covered_months` dans `flows.py`) ;
  - répartition par catégorie pour chaque nature, « Sans catégorie » inclus ;
  - les 5 plus grosses dépenses de l'année (visibles, jamais retirées des totaux : aucun seuil d'« exceptionnel » inventé).
- [ ] `GET /banking/real-cashflow/months/{period}` → même forme pour un mois terminé (400 sinon), avec la répartition par catégorie des entrées et des sorties, et `previous_period` / `next_period` bornés aux mois terminés qui ont des données.
- [ ] Tests : mois courant exclu, virement vers Livret A compté en épargne et pas en dépense, remboursement neutralisé, médiane sur mois couverts, catégorie supprimée → « Sans catégorie », devise étrangère à part. Mutation sur chacun.

## Tâche B2 — Web : vue Réel sur la page Flux

**Fichiers :** `src/pages/Cashflow.vue`, nouveaux composants sous `src/components/cashflow/` (`RealCashflowYear.vue`, `RealCashflowMonth.vue`), `src/stores/cashflow.ts` ou nouveau store, `src/types/index.ts`.

- [ ] Bascule **Visé / Réel** en tête de page. Visé = le contenu actuel. La bascule est mémorisée localement (try/catch sur le stockage).
- [ ] Réel, vue **Année** par défaut : sélecteur d'année (`years_available`, par défaut l'année courante si elle compte au moins un mois terminé, sinon la précédente) ; cartes Entrées, Dépenses, Mis de côté, Investi, avec bascule **Moyenne / Médiane** mensuelle ; barres entrées/sorties par mois ; répartition par catégorie ; les 5 plus grosses dépenses.
- [ ] Clic sur un mois → vue **Mois** : entrées et sorties par catégorie, précédent/suivant bornés aux mois terminés, retour à l'année.
- [ ] Module bancaire désactivé ou aucune opération : état vide qui renvoie vers Banque.
- [ ] Graphiques : réutiliser les composants existants de `src/components/charts/` avant d'en créer un ; si un nouveau est nécessaire, suivre la palette et les conventions de `InvestmentComparisonBarChart.vue`.
- [ ] Tests vitest : année par défaut (mois courant seul → année précédente), bascule moyenne/médiane, navigation de mois bornée.

## Tâche B3 — Masquer la comparaison visé ↔ réel

**Fichiers :** `src/pages/Cashflow.vue`.

- [ ] Retirer `<CashflowComparisonCard />` et son import de la page. **Garder** le composant, l'action du store, les routes `/cashflow/me/comparison` et `/cashflow/{id}/match`, et `matching.py`.
- [ ] Vérifier qu'aucun appel `fetchComparison` ne part plus au chargement de la page.

---

## Vérification finale

1. `uv run pytest -q` vert (sandbox désactivé), nombre de tests en hausse et noté dans le résumé de branche.
2. `pnpm type-check` et `pnpm test` verts dans `capitalview-web`.
3. Mutation faite sur chaque test neuf.
4. `docker exec capitalview-backend alembic upgrade head` puis `downgrade -1` puis `upgrade head` sur la base de dev.
5. Rejeu réel via la route d'étude : nombre de règles pour 80 % du volume, part des opérations « à ranger » après ~20 règles manuelles, et un passage IA complet si une clé est configurée. **Supprimer le dump.**
6. Contrôle visuel dans Chrome : puce et picker sur l'onglet Opérations, onglet Catégories, vue Réel année puis mois, et absence de la carte de comparaison.

## Hors périmètre (vient après)

Séries récurrentes et détection de variations, rapprochement visé ↔ réel par série ou par catégorie, page abonnements, rapprochement des sorties d'investissement avec les versements PEA et crypto de l'app, ouverture des catégories à l'agent MCP.
