# Cashflow réel : types d'opérations et abonnements — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** savoir précisément, par mois terminé et par année, combien est **entré**, **dépensé** (dont **abonnements**), **mis de côté** et **investi**, à partir des opérations bancaires, sans jamais descendre au détail des catégories de dépenses.

**Remplace** le plan `2026-09-15-operation-categories-real-cashflow.md` : catégories personnalisées, règles par mots et IA de catégorisation sont **abandonnées** (décision du 2026-09-16). Le cashflow visé garde ses catégories texte, inchangées.

**Tech Stack :** FastAPI + SQLModel + Alembic + pytest (`capitalview-api`) ; Vue 3 `<script setup>` + Pinia + Tailwind v4 + vitest (`capitalview-web`).

---

## Décisions (2026-09-16)

- **Aucune catégorisation des dépenses.** Pas de catégories bancaires, pas de règles par mots, pas d'IA.
- **Types exclusifs**, un seul par opération :
  | Type | Compte dans | Sens |
  |---|---|---|
  | `INCOME` Entrées | Entrées | crédit +, débit − |
  | `EXPENSE` Dépenses | Dépenses | débit +, crédit − |
  | `SAVING` Épargne | Mis de côté | débit + (mis de côté), crédit − (repris) |
  | `INVESTMENT` Investissement | Investi | débit +, crédit − |
  | `NEUTRAL` Neutre | nulle part | virements entre ses comptes, annulations, rejets, recharges, prêts et avances |
- **Remboursement** : une réponse, pas un type. Il se range en `EXPENSE` côté crédit, donc **déduit des dépenses** du mois où il arrive (ex. un ami qui rembourse sa part).
- `NEUTRAL` n'est **jamais deviné** : il vient d'une paire détectée ou d'une réponse de l'utilisateur.
- **Épargne et investissement sont des virements** ; tout le reste est dépense (débit) ou entrée (crédit). Pas de file de relecture des libellés.
- **Épargne nette du mois = versé vers les comptes d'épargne − repris depuis ces comptes** : paires détectées vers un compte d'épargne de l'app, plus les réponses « Épargne » / « Reprise d'épargne » pour les comptes absents de l'app. Un virement entre deux comptes d'épargne ne compte pas ; les intérêts sont une Entrée. Même logique pour l'investissement.
- **Questions de flux** (à l'endroit des virements douteux), une fois par libellé :
  - chaque **crédit non apparié** → Entrée, Remboursement, Reprise d'épargne, Reprise d'investissement, Neutre ;
  - chaque **virement émis non apparié** → Dépense, Épargne, Investissement, Neutre.
  La réponse vaut pour le libellé et pour les libellés **proches** (≥ 60 % de mots informatifs communs, même compte, même sens : mécanisme de `transfer_decisions`), sinon un salaire à référence changeante redemanderait chaque mois. Une dépense carte ne pose jamais de question (≈ 2 000 opérations).
- Un virement vers un compte **présent dans l'app** est reconnu par l'appariement existant ; tout le reste passe par les questions ou le picker.
- **Abonnement = une marque sur une Dépense**, pas un type : les Dépenses incluent les abonnements, l'écran affiche « dont abonnements ». Abonnement = **toute charge récurrente** (streaming, forfait, sport, mais aussi loyer, énergie, assurance, crédit).
- **Abonnements détectés puis confirmés** au même endroit que les virements douteux : sur la ligne dans Opérations, Oui / Non, compté dans le badge et le filtre « à vérifier ». Le refus est retenu.
- **Correction du type** : par **libellé** (le libellé exact et les libellés proches du même compte et du même sens, passés et futurs, coché par défaut) ou **par opération**. Jamais par mots isolés.
- Le **moyen de paiement** (`operation_type` : carte, virement, prélèvement…) est conservé : affichage, filtre, et **seulement** pour décider si un débit pose une question de flux (R4b). Jamais lu par un total ni par la détection d'abonnements.
- **Rien n'est en production** ni poussé. La migration `b1c2d3e4f5a6` de l'ancien plan a été supprimée et la base de dev redescendue à `28fdf21d9e17` (tête de `main`) le 2026-09-16 : une seule migration neuve sera créée.

## Amendements après revue (2026-09-16)

- **Deux lots.** Lot 1 : R0, R1 (sans `bank_subscriptions`), R2, R3, R4, R4b, R6, W0, W1, W2, W4 — types, questions de flux, Réel. Lot 2 : R5, W3 et la table `bank_subscriptions` — abonnements, plus heuristiques, livrés ensuite. En lot 1, `subscriptions`, `is_subscription`, `subscription_*` et le badge « Abonnement » sont absents, pas stubés.
- **Fraîcheur.** `transfer_patterns.source_digest` inclut `bank_type_rules` (nombre, `max(created_at)` ; remplacer une règle = supprimer puis insérer) et, en lot 2, `bank_subscriptions` (nombre, `max(updated_at)`). Test (mutation) : répondre à une question fait tomber le total de `/banking/transfer-questions` sans qu'aucune opération ne change.
- **Questions de flux calculées sur l'historique.** La reconstruction des patterns calcule, par groupe (compte, sens, signature) sans règle exacte ni proche : l'opération porteuse (la dernière finale) et le nombre d'opérations du groupe par mois. Stocké dans les patterns (chiffrés). La liste du mois pose `flow_question` sur la porteuse ; les puces et le badge comptent la question au mois de la porteuse ; `open_questions` d'une période du Réel compte les opérations de la période dont le groupe a une question ouverte, plus les paires `suggested` de la période.
- **Libellé proche, définition exacte.** Mots = `label_words` ; mots informatifs = mots − `patterns.common(compte, sens)` (> `COMMON_WORD_SHARE` des opérations du côté, ≥ `COMMON_WORD_MIN_COUNT`) ; score = Jaccard (∩ / ∪) des mots informatifs de la ligne et de la règle ; proche si score ≥ `transfer_decisions.SIMILARITY_THRESHOLD`. Plusieurs règles proches : meilleur score, puis la plus récente. Aucun mot informatif → jamais proche.

## Mesures qui fondent le plan (dump réel du 2026-09-16)

4 263 mouvements, 4 comptes (Bourso courant, Revolut, Livret A, LDDS), tous en EUR. 3 624 opérations finales non appariées (hors `recurring`, `savings`, `refund`) ; 52 paires encore `suggested`.

- **Sorties** : 3 102 opérations, 110 790 €, 984 libellés ; **entrées** : 522 opérations, 114 241 €, 130 libellés.
- **Virements vers des comptes absents de l'app, comptés à tort en dépenses par défaut** : `VIR INST ROUKINE EMILIEN` −4 710 € (23), `To Emilien Roukine` −1 558 € (10), `VIR Virement de Emilien ROUKINE` −1 442 € (17), `VIR Virement interne depuis BoursoBank` −2 066 € (16), `… depuis Compte courant` −2 265 € (10). Isolés : `VIR SEPA Jean-louis Demenge` −19 000 € et −1 000 €, `REJ VIR INST ROUKINE EMILIEN` +2 000 €.
- **Remboursements entre proches** : `Virement de : TITOUAN RATTIN` +1 121 € (25), `HUGO BOUVERAT` +313 € (9)…
- **Volume des questions** : virements reçus non appariés 469 opérations / 103 libellés / 110 255 € ; tous crédits non appariés ~130 libellés ; virements émis non appariés 342 opérations / 63 libellés / 53 970 €.
- **Cas limites mesurés** (tous couverts par les questions) : avoirs carte non appariés 15 ops / 804 € (→ Remboursement) ; « Retrait depuis une pocket » Revolut 157 € (→ Reprise d'épargne) ; recharge Apple Pay non appariée 20 € (→ Neutre) ; `REJ VIR INST` +2 010 € (→ Neutre) ; primes de parrainage 1 080 € et ventes Vinted (→ Entrée) ; intérêts de livret 64 € (→ Entrée) ; salaire VILMORIN dont la signature change chaque mois depuis 2026-06 (→ héritage par libellé proche). Investissement payé par carte (Bitstack 112 €, Binance, Coinbase) : pas de question, picker par libellé.
- **Signature trop fine pour les séries** : une même charge change de libellé. Loyer `VIR SEPA TRANSALP'DOME S.A.S.` / `… Virement pour le loyer de Emil…` / `VIR INST TRANSALP DOME S A S` ; Olness sur 3 signatures ; EDF, MACIF, Allianz sur 2.
- **Détection par signature seule** (≥ 3, cadence régulière, montant ±10 % de la médiane) : 14 séries, manque le loyer, Orange, Bouygues, MACIF (hausses de prix).
- **Détection par chaînage** (même compte, un mot commun, cadence, montant ±20 % d'une échéance à la suivante) : retrouve loyer, EDF, MACIF, Bouygues, mais **bruit** : `food` (Food Star / King / Gare), `le`, `da easy`, SNCF, Leclerc, McDo. → il faut un mot **rare** et une **exclusivité**.

## Global Constraints

- **Branche** : `feat/operation-categories` dans chaque repo (la branche `local/operation-categories-verification` y a été ramenée puis supprimée). Nouveaux commits par-dessus ; le code des catégories et de l'IA est retiré par ces commits.
- **Migration** : **une seule**, neuve, dont `down_revision = '28fdf21d9e17'`. Tant que R0/R1 ne sont pas faits, les modèles déclarent encore des colonnes absentes de la base : les écrans Banque sont cassés en dev, c'est attendu.
- **Outillage de debug local** (hors git via `.git/info/exclude`) : `routes/dev_debug.py` (`/dev/user/summary`, `/dev/user/export`, `/dev/banking/study`), `scripts/debug_db.py`, `tests/routes/test_dev_debug.py`. Le montage dans `main.py` est une modification **locale à ne jamais committer** : committer par chemins explicites, jamais `git add -A` ni `git commit -a`. Un dump écrit dans `.pytest_cache/` est supprimé après usage.
- Tout chiffré (`encrypt_data`) ; recherches par `hash_index`. **Aucune valeur jointable en clair avec `bank_transactions`** dans une table portant `user_uuid_bidx` : une règle ou un abonnement ne stocke jamais `label_signature_bidx` ni `account_id_bidx` en clair (sinon on relie un utilisateur à ses opérations). Unicité par un HMAC dérivé : `hash_index(f"type-rule:{account_id}:{credit}:{signature}", master_key)`.
- Tables sans colonne utilisateur (`bank_transactions`) : n'en gagnent pas.
- Commentaires anglais, rares, le *pourquoi*. Conventional commits anglais, **3 lignes max**, aucune mention d'outil ou d'assistant.
- `uv run pytest -q` (sandbox désactivé) ; web : PATH node puis `pnpm type-check` et `pnpm test`. **Mutation** sur chaque test neuf.

---

# Partie R — API

## Tâche R0 — Retirer catégories et IA

**Fichiers :** voir « Ce qui est retiré » en fin de plan.

- [ ] Base de dev déjà à `28fdf21d9e17`, migration de l'ancien plan déjà supprimée (vérifier `alembic current` et `alembic heads`).
- [ ] Supprimer services, routes, DTO, modèles, réglage `ai_categorization_enabled`, export/purge et tests liés aux catégories, règles par mots, file « à ranger » et IA.
- [ ] `real_cashflow.py` et `flows.py` compilent encore (la répartition par catégorie est retirée ici, la nature est remplacée en R3).
- [ ] `pytest` vert.

## Tâche R1 — Schéma

**Fichiers :** nouvelle migration (`down_revision = '28fdf21d9e17'`), `models/banking.py`, `models/__init__.py`, `models/user.py`, `services/account_data.py`.

- [ ] `bank_transactions.operation_type_enc` (conservé).
- [ ] `bank_transactions.type_override_enc` : type forcé sur **cette** opération, NULL sinon (remplace `category_ref_enc`).
- [ ] Table `bank_type_rules` : `uuid`, `user_uuid_bidx` (index), `signature_enc`, `rule_bidx` = `hash_index(f"type-rule:{account_id}:{credit}:{signature}")`, `account_ref_enc` (uuid du compte), `credit_enc` (sens), `words_enc` (mots informatifs, JSON, pour la proximité), `type_enc`, `created_at`. Unique `(user_uuid_bidx, rule_bidx)`. Sert au picker « par libellé » **et** aux réponses aux questions.
- [ ] Table `bank_subscriptions` : `uuid`, `user_uuid_bidx` (index), `account_ref_enc` (uuid du compte), `signatures_enc` (JSON des signatures membres), `status_enc` (`confirmed|refused`), `cadence_enc` (`weekly|monthly|quarterly|yearly`), `created_at`, `updated_at`.
- [ ] Plus de `bank_categories`, `bank_category_rules`, `category_ref_enc`, `ai_categorization_enabled`.
- [ ] Purge et export de compte couvrent les deux tables (mutation : retirer le `wipe` rougit le test).
- [ ] Tests : upgrade / downgrade / upgrade ; purge ; export.

## Tâche R2 — Moyen de paiement

Conservé : `services/banking/operation_types.py`, écriture dans `transactions._apply`, rattrapage dans `transfer_patterns`, `test_banking_operation_types.py`.

- [ ] **Corriger le trou vu en vérification** : une ligne dont `operation_type_enc` est NULL (jamais rattrapée, ex. après un aller-retour de migration) retombe sur `operation_type(label)` calculé à la lecture au lieu de `UNKNOWN`. Test : ligne NULL + libellé `CARTE …` → `CARD` sans reconstruction (mutation).

## Tâche R3 — Résolution du type

**Fichiers :** nouveau `services/banking/cashflow_types.py` (pur), `services/banking/type_rules.py` (stockage), `services/banking/flows.py`, `dtos/banking.py`.

- [ ] `resolve_type(movement, leg, override, rule_type, savings_accounts) -> Resolution(type, source)`, `source ∈ {pair, override, rule, default}`, dans cet ordre **exact** :
  1. **Paire déduite** (`savings`, `recurring`, `learned`, `confirmed`) :
     - exactement un des deux comptes est d'épargne (`SAVINGS`, `LIVRET_A`, `LIVRET_DEVE`, `LEP`, `LDD`, `PEL`, `CEL`) → `SAVING`, compté sur la jambe **hors épargne** uniquement ;
     - sinon → `NEUTRAL`, compté sur le débit uniquement (montant informatif).
     `reversal` / `refund` → `NEUTRAL`. Une paire ignore dérogation et règle : on la défait par les décisions de virement existantes.
  2. **Dérogation** de la ligne.
  3. **Règle** : d'abord celle du libellé exact ; sinon celle d'un libellé **proche** (même compte, même sens, ≥ 60 % de mots informatifs communs, mots trop fréquents du côté compte/sens ignorés — mêmes seuils que `transfer_decisions.SIMILARITY_THRESHOLD`) ; à égalité, la plus récente.
  4. **Défaut** : débit → `EXPENSE`, crédit → `INCOME`. Une paire `suggested` suit le défaut tant qu'elle n'est pas tranchée.
- [ ] Règles chargées **une fois par requête** : index exact `{rule_bidx: règle}` plus la liste par (compte, sens) pour la proximité ; la signature de chaque ligne se calcule dans la passe où le libellé est déjà déchiffré.
- [ ] `BankTransactionItem` : retire `nature`, `category_*`, `rule_id` ; gagne `cashflow_type`, `type_source`, `is_subscription`, `subscription_id`, `subscription_question` (voir R5).
- [ ] Tests purs, un par branche, mutation sur chacun : courant → Livret A = `SAVING` compté sur le courant seulement ; LDDS → Livret A = `NEUTRAL` ; courant → Revolut `recurring` = `NEUTRAL` ; `refund` = `NEUTRAL` ; dérogation bat règle ; règle exacte bat règle proche ; règle proche à 59 % ignorée, à 60 % appliquée ; autre compte ou autre sens ignoré ; règle bat défaut ; paire bat dérogation ; `suggested` = défaut ; crédit sans rien = `INCOME` ; crédit `EXPENSE` (remboursement) = dépense négative.

## Tâche R4 — Corriger un type

**Fichiers :** `routes/banking.py`, `services/banking/type_rules.py`, `dtos/banking.py`.

- [ ] `PUT /banking/transactions/{id}/type` — `{type, scope: "label" | "operation"}` :
  - `label` → crée ou remplace la règle de la signature, **efface** la dérogation de cette ligne ; réponse : l'opération à jour et le nombre d'opérations que la règle couvre (**hors paires**).
  - `operation` → écrit la dérogation.
  - Opération appariée (hors `suggested`) → 409, message renvoyant vers la décision de virement.
  - Opération sans libellé et `scope=label` → 400.
- [ ] `DELETE /banking/transactions/{id}/type` (retire la dérogation), `GET /banking/type-rules` (signature, type, nombre d'opérations), `DELETE /banking/type-rules/{id}`.
- [ ] Tests de routes : 404 opération d'un autre utilisateur ; 409 sur paire ; « corriger au libellé → toutes les autres suivent, y compris une opération importée ensuite ».

## Tâche R4b — Questions de flux

**Fichiers :** `services/banking/flows.py`, `services/banking/type_rules.py`, `routes/banking.py`, `dtos/banking.py`.

- [ ] Une opération finale **non appariée** (ou `suggested`) sans dérogation ni règle (exacte ou proche) porte `flow_question` si :
  - c'est un **crédit** (tout moyen de paiement) → choix `INCOME` (Entrée), `EXPENSE` (Remboursement), `SAVING` (Reprise d'épargne), `INVESTMENT` (Reprise d'investissement), `NEUTRAL` ;
  - c'est un **débit** dont le moyen de paiement est `TRANSFER` → choix `EXPENSE`, `SAVING`, `INVESTMENT`, `NEUTRAL`.
  Exception assumée au principe « le moyen de paiement ne sert qu'à l'affichage » : il décide **seulement si une question est posée**, jamais d'un montant. Un format raté = une question en moins, le défaut s'applique.
- [ ] Une question par libellé : seule la **dernière** occurrence d'un groupe (signature, compte, sens) la porte ; répondre crée la règle (R4, `scope=label`) et fait disparaître la question du groupe et des libellés proches.
- [ ] Une paire `suggested` garde sa question de virement ; la question de flux n'apparaît qu'une fois la paire refusée.
- [ ] Comptées dans `GET /banking/transfer-questions` (total et mois) et dans `transfer_questions` du mois, avec les virements douteux et les abonnements.
- [ ] Tests (mutation) : crédit carte (avoir) → question ; débit carte → aucune ; débit `TRANSFER` → question ; une seule question pour 23 `VIR INST ROUKINE EMILIEN` ; réponse → plus de question sur le groupe **ni** sur un libellé proche importé ensuite ; paire `suggested` → pas de question de flux ; réponse « Remboursement » → dépenses du mois diminuées.

## Tâche R5 — Abonnements

**Fichiers :** nouveau `services/banking/subscriptions.py` (détection pure + stockage), `services/banking/transfer_patterns.py` (cache), `services/banking/flows.py`, `routes/banking.py`.

- [ ] Entrée : débits **finals** résolus `EXPENSE`, par compte. **Mots distinctifs** d'une opération = mots du libellé moins ceux présents dans > 5 % (min 3) des débits de ce compte.
- [ ] **Série candidate** :
  1. part d'un groupe de même signature, puis absorbe les signatures du même compte qui partagent un mot distinctif **rare** (présent dans ≤ 3 signatures de l'historique) et dont les montants se suivent ;
  2. cadence par médiane des écarts : hebdo 6–8 j, mensuelle 25–36, trimestrielle 85–97, annuelle 350–380 ;
  3. ≥ 3 occurrences (≥ 2 en annuel), ≥ 75 % des écarts dans la fenêtre de la cadence ;
  4. d'une échéance à la suivante, montant à ±20 % ou ±1 € ;
  5. **exclusivité** : sur la période couverte, ≥ 80 % des débits portant le mot rare appartiennent à la série ;
  6. `active` si la dernière occurrence date de moins d'une cadence + tolérance, sinon `ended`.
- [ ] Calculée dans la reconstruction de `transfer_patterns` (même empreinte de fraîcheur, `_VERSION` incrémentée), jamais à chaque lecture.
- [ ] Une opération est `is_subscription` si sa signature appartient à un abonnement **confirmé** de son compte.
- [ ] **Question** : une série candidate ni confirmée ni refusée porte `subscription_question` sur sa **dernière occurrence** ; une nouvelle signature qui prolonge un abonnement confirmé (mot rare, cadence, montant) pose la question du **rattachement**.
- [ ] Les questions d'abonnement entrent dans `GET /banking/transfer-questions` (total et mois) et dans le `transfer_questions` du mois de `/banking/transactions`, pour que le badge, les puces de mois et le filtre « à vérifier » existants les comptent.
- [ ] Routes :
  - `POST /banking/subscriptions/decisions` `{transaction_id, decision: confirm|refuse}` : la série de cette opération ;
  - `GET /banking/subscriptions` → confirmés (actifs, terminés) : libellé de la dernière occurrence, compte, cadence, montant actuel, dernier changement de prix (date, avant → après), coût annualisé, prochaine échéance estimée, occurrences ;
  - `DELETE /banking/subscriptions/{id}` (redevient proposable).
- [ ] **Rejeu réel avant le code final**, sur le dump, liste **validée par Emilien le 2026-09-16** :
  - proposés : loyer Frederic Durand (380 €), loyer Transalp'Dome (530 €, 3 libellés), EDF (2 signatures), MACIF (2 contrats), Allianz Direct, Allsecur, Olness (2 comptes = 2 séries), Bouygues Telecom, Orange, Cinépass Pathé, Anthropic, OVH, EA ;
  - jamais proposés : SNCF, Carrefour, Leclerc, Lidl, Food Star/King/Gare, `da easy`, McDonald's, Burger King, laverie, virements à des personnes (`VIR INST <nom>`).
  Seuils ajustés jusqu'à 100 % de la première liste et 0 de la seconde, ou écart expliqué à Emilien.
- [ ] Tests purs, mutation sur chaque critère : cadence, régularité, montant, exclusivité, fusion de deux signatures, annuel à 2 occurrences, `ended`, rattachement, refus retenu, un débit `SAVING` jamais proposé, question comptée dans `transfer-questions`.

## Tâche R6 — Cashflow réel

**Fichiers :** `services/banking/real_cashflow.py`, `routes/banking.py`, `dtos/banking.py`.

- [ ] Conservé : mois **terminés** seulement, opérations en attente exclues, devise principale, autres devises à part, moyenne et médiane sur les mois couverts, navigation bornée, les 5 plus grosses dépenses.
- [ ] Retiré : toute répartition par catégorie.
- [ ] Totaux par mois et par année : `income`, `expenses`, `subscriptions` (**inclus** dans `expenses`), `saving`, `investment`, `neutral` (informatif), `net = income − expenses − saving − investment`.
- [ ] Par période : `open_questions` (virements `suggested` + questions de flux + abonnements à confirmer), pour que l'écran dise quand un chiffre peut encore bouger.
- [ ] Mois : abonnements débités ce mois.
- [ ] Tests (mutation sur chacun) : mois courant exclu ; virement vers Livret A = épargne, pas dépense ; reprise depuis livret = épargne négative ; épargne nette = versé − repris sur le mois (livret de l'app + réponse « Épargne » vers un compte absent) ; virement Livret A → LDDS hors épargne ; intérêts de livret = entrée ; remboursement déduit des dépenses ; paire `recurring` absente des totaux ; avoir carte apparié (`refund`) neutre ; règle `SAVING` sur `VIR INST <nom>` sort le montant des dépenses ; abonnement confirmé compté dans `expenses` **et** `subscriptions` ; `suggested` compté par défaut et signalé ; médiane sur mois couverts.

# Partie W — Web

## Tâche W0 — Retirer catégories et IA

- [ ] Supprimer `BankCategoryPicker.vue`, `pages/BankCategories.vue` et sa route, l'onglet Catégories, `stores/bankCategories.ts`, `utils/bankCategories.ts`, `composables/useCategoryPicker.ts`, `RealCashflowCategoryList.vue`, les entrées de `sessionReset.ts`, et leurs tests.
- [ ] `SettingsAI.vue` identique à `main`. `Cashflow.vue` (visé) : `existingCategories` redevient le calcul local de `main`.
- [ ] `api/client.ts` : garder `patch` seulement s'il sert encore ; corriger son indentation cassée (`    async delete`).
- [ ] `pnpm type-check` et `pnpm test` verts.

## Tâche W1 — Types et store

**Fichiers :** `src/types/index.ts`, nouveau `src/stores/cashflowTypes.ts`, `src/utils/cashflowTypes.ts`, `src/services/sessionReset.ts`.

- [ ] Libellés FR fixes : Entrées, Dépenses, Épargne, Investissement, Neutre ; badge « Abonnement ».
- [ ] Actions : corriger un type (libellé / opération), répondre à une question de flux, retirer une dérogation ou une règle, décider d'un abonnement, lister et retirer les abonnements. Invalide `bank:flows:*` et incrémente `bank.dataRevision` après chaque écriture.

## Tâche W2 — Opérations

**Fichiers :** `src/components/bank/BankTransactionRow.vue`, nouveau `src/components/bank/BankTypePicker.vue`, `src/pages/BankTransactions.vue`.

- [ ] Ligne : puce du type, badge « Abonnement », moyen de paiement discret. Une opération appariée garde son badge de virement existant, sans picker.
- [ ] **Question d'abonnement** sur la ligne, à côté des Oui / Non des virements douteux et avec le même style : « Abonnement mensuel · 39 € ? » Oui / Non.
- [ ] **Question de flux** sur la ligne, même style : « Ce virement reçu, c'est… » Entrée · Remboursement · Reprise d'épargne · Reprise d'investissement · Neutre (et pour un virement émis : Dépense · Épargne · Investissement · Neutre), avec « s'applique aux N opérations de ce libellé ».
- [ ] Les trois sortes de questions entrent dans le filtre « à vérifier », les puces « À vérifier aussi » et le badge de l'onglet.
- [ ] Picker : types, « Appliquer à toutes les opérations « <libellé> » et aux libellés proches » coché par défaut avec le nombre concerné ; tous les types proposés quel que soit le moyen de paiement (ex. achat carte Bitstack → Investissement), « Revenir au type détecté » si dérogation ou règle ; focus automatique.
- [ ] Filtres : type (dont « Abonnements ») et moyen de paiement, cumulés avec les existants ; le total filtré suit ; chevron des selects sans chevauchement du texte.

## Tâche W3 — Onglet Abonnements

**Fichiers :** `src/components/bank/BankTabs.vue`, `src/router/index.ts`, nouvelle page `src/pages/BankSubscriptions.vue`.

- [ ] Remplace l'onglet Catégories.
- [ ] Actifs puis terminés : montant, cadence, coût annualisé, prochaine échéance, hausse de prix signalée ; total mensuel et annuel des actifs ; retirer un abonnement.
- [ ] Le badge de l'onglet Opérations compte virements douteux, questions de flux **et** abonnements à confirmer.

## Tâche W4 — Flux : vue Réel

**Fichiers :** `src/pages/Cashflow.vue`, `src/components/cashflow/RealCashflow{View,Year,Month}.vue`, `src/components/charts/CashflowMonthsBarChart.vue`, `src/stores/realCashflow.ts`, `src/utils/realCashflow.ts`, `src/composables/useRealCashflowView.ts`.

- [ ] Conservé : bascule Visé / Réel mémorisée, année par défaut, sélecteur d'année, Moyenne / Médiane, barres par mois, clic → mois, navigation bornée, retour, 5 plus grosses dépenses, carte de comparaison masquée.
- [ ] Cartes : Entrées, Dépenses (« dont abonnements X € »), Épargne, Investissement, Reste (`net`).
- [ ] Bandeau quand `open_questions > 0` : « N points à confirmer peuvent encore changer ces chiffres », lien vers Opérations filtré « à vérifier ».
- [ ] Mois : totaux par type et abonnements débités.
- [ ] Tests vitest : année par défaut, Moyenne/Médiane, navigation bornée, bandeau.

---

## Ce qui est retiré de `feat/operation-categories`

API : `services/banking/categories.py`, `services/banking/categorize.py` (la logique épargne de `nature_of` est réécrite dans `cashflow_types.py`), `services/ai/agents/categorize_agent.py`, `WordFrequency` et `propose_tokens` (sauf ce que R5 réutilise pour le mot rare), `_Filing`, `_filed`, `_resolution`, `assign_category`, `rule_words`, `uncategorized_groups` dans `flows.py`, tables `bank_categories` et `bank_category_rules`, colonne `category_ref_enc`, `user_settings.ai_categorization_enabled` (modèle, DTO, service), routes `/categories*`, `/category-rules*`, `/uncategorized`, `/categorize/ai`, `/transactions/{id}/rule-tokens`, `/transactions/{id}/category`, et les tests `test_banking_categories.py`, `test_banking_categorize.py`, `test_banking_categorize_agent.py`, `test_banking_category_filing.py`, `test_banking_category_routes.py`, les parties catégories de `test_account_data.py` et `test_settings.py`.

Web : voir W0.

## Vérification finale

1. `uv run pytest -q` vert, nombre de tests noté ; `pnpm type-check` et `pnpm test` verts ; mutation faite sur chaque test neuf.
2. Migration upgrade / downgrade / upgrade en dev, **puis** une lecture qui force la reconstruction des patterns, et contrôle que `operation_type_enc` est rempli.
3. Base : aucun type, aucune signature, aucun uuid de compte en clair dans `bank_type_rules`, `bank_subscriptions`, `type_override_enc` ; aucune colonne jointable en clair avec `bank_transactions` ; plus de table `bank_categories` ni `bank_category_rules`.
4. Rejeu réel (via `/dev/banking/study` ou `scripts/debug_db.py`, dump supprimé ensuite) :
   - abonnements : liste validée de R5 atteinte ;
   - toutes les questions de flux répondues (≈ 63 libellés émis, ≈ 130 reçus), puis 2025 contrôlé **à la main** : Entrées, Dépenses, Épargne, Investissement comparés aux relevés, écart expliqué ligne à ligne ;
   - temps de `/banking/transactions`, `/banking/transfer-questions`, `/banking/subscriptions`, `/banking/real-cashflow` notés (référence 2026-09-15 : réel année ~60 ms).
5. Contrôle visuel Chrome, desktop puis ~400 px : Opérations (puce, picker, question d'abonnement, filtres), Abonnements, Réel année et mois, bandeau.
6. `git status` : `main.py` jamais committé avec le montage du routeur de debug ; aucun fichier de `.git/info/exclude` suivi.

## Hors périmètre

Catégories de dépenses sous quelque forme que ce soit, IA, rapprochement automatique d'une sortie avec un versement PEA/crypto de l'app, rapprochement visé ↔ réel.
