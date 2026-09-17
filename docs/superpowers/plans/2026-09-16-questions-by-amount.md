# Cashflow réel : des chiffres justes vite, puis tout ce que les données savent dire — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal :** (1) rendre l'écran Réel juste avec le moins de réponses possible ; (2) faire dire aux opérations typées tout ce qui aide à décider (taux d'épargne, matelas, rythme du mois, d'où vient et où va l'argent, comparaison à l'an dernier) ; (3) une page **Explorer** où l'on filtre, regroupe, compare et descend jusqu'à l'opération, sur tout l'historique.

**Suite de** `2026-09-16-real-cashflow-types-subscriptions.md` (lot 1 livré). **Les abonnements** restent le lot 2 de ce plan-là, autre session ; ce plan ne les touche pas, mais la page Explorer et le Réel sont pensés pour les accueillir.

**Tech Stack :** FastAPI + SQLModel + pytest (`capitalview-api`) ; Vue 3 `<script setup>` + Pinia + Tailwind v4 + ECharts + vitest (`capitalview-web`).

---

## Compte rendu d'exécution (2026-09-16)

**État** : parties Q, C, L, R et X codées et testées sur `feat/operation-categories` dans les deux repos, **rien n'est commité**. Contrôle visuel dans Chrome fait en partie (voir « Reste à faire »).

**Tests** : API 1 473 → **1 538** (`uv run pytest -q`, vert, relancé après coup) ; web 126 → **172** (`pnpm type-check`, `pnpm test`, `pnpm build-only` verts). Mutation faite sur chaque test neuf, et chaque survivant a reçu un test. Il reste un seul survivant, équivalent : dans `real_cashflow.py`, sur le contrôle « compte périmé », `<= J-8` revient à `< J-7` pour des dates.

**Fichiers neufs** — API : `services/banking/label_groups.py`, `services/banking/ledger.py`, `tests/services/test_banking_{label_groups,ledger,real_cashflow_insights,review_queue}.py`, `tests/routes/test_banking_insight_routes.py`. Web : `pages/BankReview.vue`, `stores/ledger.ts`, `utils/ledger.ts`, `composables/useExploreFilters.ts`, `components/cashflow/RealCashflow{Counterparts,Coverage,Pace}.vue`, `components/cashflow/explore/*` (6 composants), specs associées.

### Mesures sur le dump (lecture seule, `/tmp` du scratchpad)

| Route (service) | Temps |
|---|---|
| reconstruction des patterns (`_VERSION` 8) | 316–331 ms |
| `/banking/review-queue` | 270–283 ms |
| `/banking/real-cashflow` (année) | 142–151 ms |
| `/banking/real-cashflow` (mois) | 36–42 ms |
| `/banking/real-cashflow/current` | 90–93 ms |
| `/banking/ledger` | 288–306 ms ; calcul de l'ETag seul 9–13 ms |

- **Seuil 100 €** (Q4 ci-dessus). File avant toute réponse : **127 questions, 165 314 €** (2026 : 56 / 81 562 € ; 2025 : 27 / 60 818 €).
- **Après les 9 règles saisies dans l'app le 2026-09-16 vers 20 h 37** (NETDEV, Serge Roukine ×2, Dormia Laure ×2, Vilmorin, Workera en Entrée ; Frederic Durand, Baumet Elise en Neutre) : **110 questions, 78 409 €**. Ces règles sont des réponses réelles, pas des résidus de test : ne pas les effacer.
- **Grand livre** : 4 263 lignes, 1 081 groupes de libellé, **1 662 Ko de JSON, 203 Ko gzip** (plus que les 942 / 152 Ko estimés : les champs définitifs de L1 sont plus nombreux).
- **Parité grand livre ↔ Réel sur 2025** : identique au centime (entrées 41 715,41 €, dépenses 45 253,27 €, neutre 8 431,81 €, épargne −1 503,50 €).
- **Réel 2026** (avec les 9 règles) : taux d'épargne 11,6 %, dont placé 1,7 % ; matelas 1,5 mois (3 451 € / 2 324 € par mois ; livrets 1,3 mois ; Epargne et Plaisir signalés périmés) ; mois atypiques : 2026-06 et 2025-06 ; trous de couverture : Plaisir (fin au 05/01/2026) sur 2026, Epargne (début tardif) sur 2025.
- **Rythme au 16 septembre** : 2 557 € dépensés dont 216 € en attente ; médiane au 16 : 1 227 € ; médiane d'un mois : 2 324 € ; fin de mois estimée 3 654 €.
- **Proxy de prod** : `capitalview-infra` ne contient aucune configuration de proxy ni de compression. La compression en prod n'est donc **pas vérifiée** ; `GZipMiddleware` est en place quoi qu'il en soit.

### Écarts au plan

- **C1** : pas de liste `coverage` complète servie. Seuls les trous sont servis, en `coverage_gaps: [{account_id, account_name, first_day, covered_until, starts_late, ends_early}]`. `covered_until` = dernière synchro d'un compte lié, dernière opération d'un compte importé.
- **L3** : pas de `getWithEtag` dans `api/client.ts`. La route envoie `ETag` + `Cache-Control: private, no-cache` : le navigateur revalide seul et le 304 lui revient de façon transparente. Le store recharge sur `dataRevision` et ignore une réponse dépassée par une requête plus récente.
- **X2** :
  - pas de brosse de plage sur le graphique. Cliquer une barre restreint bien la période à ce mois.
  - sur mobile, les filtres se replient derrière un bouton sous la recherche et la période, pas dans un tiroir plein écran.
  - `useExploreFilters` : pas de jsdom ni `@vue/test-utils` dans le projet. La logique URL ↔ filtres est sortie en fonctions pures (`serializeState` / `parseState`), testées, plutôt que d'ajouter ces dépendances.
- **X2, barre de période** : le sélecteur de préréglages a sa propre largeur et défile horizontalement sur téléphone (sinon chaque libellé passait sur deux lignes).

### Reprise du 2026-09-17

**Performance de l'Explorer** (mesurée dans Chrome, serveur Vite de dev, onglet en arrière-plan) : 110 à 350 ms par changement de filtre au départ.
- **`groupBy` par mois ou semaine** : 120 à 430 ms. `monthLabel` et le libellé de semaine créaient un formateur `Intl` par ligne. Formateurs construits une fois → 15 ms par mois, 20-35 ms par semaine.
- **Filtrage** : la recherche était repliée (NFD + regex) à chaque ligne, les filtres relus à travers le proxy réactif, et ce 4 à 5 fois par changement. `matcher(filters)` prépare tout une fois par passe. Calculs d'un changement : 50-70 ms → **10-20 ms**.
- **Reste le rendu, 70 à 160 ms en dev** : liste des opérations ~46 ms (50 lignes), répartition ~28 ms, graphique ~20-28 ms, vue ~13 ms. **Non mesuré en build de prod**, où Vue est plusieurs fois plus rapide : l'API n'accepte que l'origine `localhost:5173` (CORS), donc pas de `vite preview` à côté du serveur de dev. L'objectif < 50 ms reste **non vérifié**. Piste si la prod ne suffit pas : `useFormatters` crée aussi un `Intl.NumberFormat` / `DateTimeFormat` par appel (~0,05 ms par montant, ~0,1 ms par date), dans toute l'app.

**Corrigé en contrôlant**
- **Groupes de libellé** : un paiement carte sans mot de 2 lettres ou plus (« H&L 2 », « A.R.E.A. », « G T I ») retombait sur la signature « carte cb », partagée par tous. Sur le dump, 9 opérations et 131 € réunies sous « A.R.E.A. ». La clé devient le nom nettoyé (`label_groups.group_key`).
- **ETag du grand livre** : il ne dépendait que des données. Un changement de lecture (comme le précédent) n'atteignait pas un navigateur qui avait déjà le grand livre (304). `ledger.LEDGER_VERSION` entre dans l'ETag, à incrémenter à chaque changement de ce que les lignes disent.

**X3, contrôlé** : mode confidentialité (graphique masqué via `format`), export CSV (BOM, `;`, virgule décimale, 1 133 lignes pour 1 133 opérations), correction d'un type depuis la liste de l'Explorer pour une seule opération (Vinted 80 € → Neutre, écran mis à jour) puis « Revenir au type détecté ». Base vérifiée ensuite : 9 règles, aucune dérogation.

**Tests** : API 1 540, web 172, verts ; mutation sur les tests ajoutés.

### Revue avant fusion (2026-09-17)

Relecture des calculs et des écrans avec les vraies données. Rien de faux trouvé ; quatre points de lisibilité, traités sauf mention.

- **Une contrepartie tenait plusieurs lignes.** « Vilmorin & Cie » et « Vilmorin & Cie Salaire De » comptaient pour deux sources (26 % + 16 %). Deux changements dans `label_groups` :
  - `NOISE_WORDS` : la plomberie bancaire (`sepa`, `prlv`, `tdf`, `emis`, `via`…) et les mots outils sortent de la clé. `label_common` ne les attrapait pas tous : « TDF EMIS VIA CB » ne touche que 36 opérations, et laissait « Revolut » et « Lydia » partager trois quarts de leur clé.
  - `merge_similar` : deux groupes qui partagent assez de mots sont la même contrepartie, au seuil déjà utilisé pour les libellés proches (`SIMILARITY_THRESHOLD`, 0,6). Le groupe qui porte le plus d'opérations donne son nom. Seuls les groupes partageant un mot sont comparés : 1 451 comparaisons sur le dump, temps de `/banking/ledger` inchangé (296 ms).
  - **Mesuré avant de choisir** : à 0,6 sur clés nettoyées, 38 groupes absorbés sur 1 051, toutes fusions justes (Vilmorin, Amazon, Burger King, PayPal, MACIF, loyer Transalp'Dome, virements personnels). À 0,5 et avec la règle « mots inclus » (223 groupes absorbés), « Carrefour Annecy » avalait « Annecy », « Lyon » et les retraits d'espèces : écartés.
  - Résultat sur 2026 : Vilmorin devient une source unique à 42,1 %, et le grand livre passe de 1 081 à 1 028 groupes. Parité avec le Réel toujours exacte.
- **Noms tronqués dans le diagramme des flux.** La cause n'était pas la troncature mais le seuil « compact » : il se déclenche sous 768 px **de largeur de graphique**, or une carte sur un écran de 1 400 px n'en fait que 746. La largeur des noms suit maintenant celle du graphique (30 %, entre 72 et 280 px). Vérifié en 1 400 px et en 390 px.
- **La tuile « Solde » de l'Explorer** lit comme une perte alors qu'une partie n'a été que mise de côté (l'autre jambe du virement est hors sélection). La ligne qui existait déjà dit maintenant « dont X mis de côté ». Pas de bloc en plus.
- **Ce que fait chaque réponse de type** est expliqué au survol (`answerHint`, attribut `title`), sur la question de flux comme dans la fenêtre du type. Aucune place prise à l'écran : l'interface reste minimale.

`LEDGER_VERSION` passée à 3 (la lecture des groupes a changé). Tests : API 1 544, web 173, verts ; mutation sur chaque test neuf.

### Reste à faire

1. Mesurer l'Explorer sur le build de prod (après déploiement, ou en autorisant une seconde origine en dev).
2. Chaque préréglage de période contrôlé un par un à l'œil.

---

## Avis sur la revue du 2026-09-16

**D'accord sur l'essentiel.** Le modèle (un type par opération, règles par libellé, questions sur virements et crédits seulement) est au bon endroit ; le tri par montant, le seuil et l'incertitude en euros sont le plus gros gain immédiat ; les abonnements sont le prochain meilleur rapport valeur/effort ; pas de catégories.

**Ce que la revue a manqué, mesuré sur le dump (voir « Mesures ») :**

1. **L'investissement est à 0 € partout** tant que les virements émis ne sont pas classés. Envisagé puis **écarté le 2026-09-16** : rapprocher ces virements des versements saisis sur les comptes d'investissement de l'app (56 sur 62 correspondaient). Trop lié à une façon de tenir ses comptes ; la voie reste générale : un virement que rien ne reconnaît (ni paire, ni récurrence) pose sa question de flux, et la réponse « Investissement » le classe, lui et les libellés proches.
2. **Certaines années sont fausses par couverture, pas par manque de réponses.** Le Livret A n'a d'opérations qu'à partir de 2025-01, le LDDS s'arrête en 2026-01, le Livret A n'est plus à jour depuis le 31/08 (d'où une question sur un virement « depuis Compte épargne » du 15/09 qui est en fait une paire). Avant 2025, un virement vers le livret ne peut pas s'apparier et compte en dépense. Le Réel doit le dire (Partie C).
3. **« Taux d'épargne = (épargne + investissement) / entrées » sous-estime.** Ce qui reste sur le compte courant est aussi épargné. Deux taux : **taux d'épargne** = (entrées − dépenses) / entrées (le chiffre phare), **dont placé** = (épargne + investissement) / entrées.
4. **« Fixe contre variable par moyen de paiement » : à ne pas faire.** Les prélèvements pèsent 4,9 k€ sur 4 ans alors que le loyer (530 €/mois) passe par virement : l'approximation serait trompeuse. Le fixe viendra des abonnements (lot 2).
5. **Le rythme du mois en cours** manque. Le Réel exclut à raison le mois en cours ; mais « 1 073 € dépensés au 20, médiane des 12 derniers mois au 20 : 1 073 € » est le chiffre le plus actionnable au quotidien, et il est calculable.
6. **D'où vient l'argent** est le miroir gratuit de « où va l'argent » : 130 libellés d'entrées, les 5 premiers font 53 % des entrées.
7. **Le visé peut se comparer ligne par ligne**, pas seulement par type : le visé a 15 lignes nommées (« Loyer », « Papa », « Salaire »…). Rattacher une ligne visée à un libellé bancaire = 15 réponses, pas une catégorisation. À faire après les abonnements (hors plan, noté en fin).

**Ce qui a été mesuré et ne vaut pas l'effort :** frais bancaires (17 opérations, 115 € en 4 ans) ; doublons (63 cas, tous des achats répétés légitimes : laverie, bus) ; jour de la semaine ; fusion manuelle de commerçants (la recherche « carrefour » couvre déjà « Carrefour Annecy », « CARREFOUR.FR » et « Carrefour » Revolut).

## Mesures (dump réel, 2026-09-16, lecture seule)

53 mois, 4 263 opérations, 4 comptes bancaires (Principal et Secondaire courants, Epargne Livret A, Plaisir LDDS), 1 PEA, 1 portefeuille crypto. **Aucune règle de type en base** : tous les chiffres ci-dessous sont au défaut.

- **Questions** : 186 questions de flux + 26 paires `suggested` = 212 au badge ; 166 591 € en jeu. 29 réponses → 80 % des euros, 47 → 90 %, 69 → 95 %. 67 questions < 50 € = 1 137 € (0,7 %) ; 104 < 200 € = 5 000 € (3 %).
- **Plus grosses questions** : `VIR SEPA JEAN-LOUIS DEMENGE` −20 000 € (2, achat de la Corolla), `NETDEV` +13 466 € (13), `SERGE ROUKINE` +13 333 € (1) et +12 078 € (36), `VILMORIN` +11 771 € (9, plus 3 signatures isolées), `DORMIA LAURE` +9 562 € (42), `VIREMENT DEPUIS COMPTE COURANT` −4 807 € (21), `VIR INST ROUKINE EMILIEN` −4 710 € (23).
- **Réel au défaut** : 2025 entrées 44 853 €, dépenses 46 013 €, épargne −1 504 €, investissement 0 € ; 2026 (8 mois) épargne 450 €, investissement 0 €.
- **Virements vers les comptes d'investissement** : saisis dans l'app (PEA, crypto), ils partent de 4 à 5 libellés du compte Principal (`VIR Virement depuis Compte courant` 21 op. / 4 807 €, `… interne depuis BoursoBank` 15 / 2 046 €, `… interne depuis Compte cour…` 10 / 2 265 €, une partie de `VIR INST ROUKINE EMILIEN`) : quelques réponses « Investissement » suffisent.
- **Couverture** : Principal 2022-10 → 2026-09-16, Secondaire 2022-05 → 2026-09-02, Epargne (Livret A) 2025-01-28 → 2026-08-31 (non synchronisé), Plaisir (LDDS) 2024-08-26 → 2026-01-05 (non synchronisé).
- **Dépenses carte et Revolut** : 828 groupes de libellés (mots informatifs), 47 771 € ; 10 premiers = 25 %, 20 = 35 %, 50 = 52 %, 100 = 67 %. Exemples : Carrefour Annecy 93 passages / 21,6 € ; CROUS resto 196 / 3,2 € ; Vinted 21 / 54,5 € ; SNCF 56 / 8,2 €.
- **Rythme** : dépenses carte + espèces + prélèvements, médiane des 12 derniers mois terminés : 489 € au 10, 1 073 € au 20, 1 554 € en fin de mois. Dépenses totales mensuelles (défaut) sur 12 mois : médiane 2 514 €, de 1 591 € à 6 260 €.
- **Soldes** (historique quotidien présent pour chaque compte) : Principal, minimum mensuel sur 12 mois de 48 € à 375 €, 9 jours sous 100 € ; Livret A de 100 € à 5 000 €.
- **Espèces** : 230 € (2022), 967 €, 2 221 € (2024), 185 €, 530 € (2026).
- **Pockets Revolut** (« Sur la Pocket EUR coffre1 depuis EUR ») : 85 débits / 188 €, typés Dépense, sans question (moyen de paiement inconnu). Négligeable, non traité.
- **Grand livre complet** (4 263 lignes typées, champs de la Partie L) : 942 Ko de JSON, **152 Ko gzip**. Aucune compression aujourd'hui (pas de `GZipMiddleware` dans `main.py` ; celle du proxy de prod **non vérifiée**).

## Décisions

1. **Ordre** : données justes d'abord (Q, C), puis grand livre (L), Réel enrichi (R), Explorer (X). Chaque partie est livrable seule.
2. **Investissement par les questions de flux**, comme aujourd'hui : aucun rapprochement avec les comptes d'investissement de l'app.
3. **File « À trier »** : un onglet Banque qui liste toutes les questions de l'historique (flux et paires `suggested`), **triées par montant en jeu décroissant**, filtrables par année, et qui se vide au fil des réponses. Opérations garde ses questions sur les lignes et ses puces de mois.
4. **Seuil** : un groupe (compte, sens, signature) dont le total sur l'historique est sous `FLOW_QUESTION_MIN_AMOUNT` ne pose pas de question ; le défaut s'applique. Le picker reste disponible. Un groupe qui dépasse le seuil plus tard se met à demander. Les paires `suggested` ne sont **pas** concernées.
5. **Incertitude en euros** : « X € restent à confirmer » par année et par mois, à côté du nombre.
6. **Taux d'épargne** = (entrées − dépenses) / entrées ; **dont placé** = (épargne + investissement) / entrées. `null` si entrées ≤ 0.
7. **Grand livre servi au client** : une route renvoie l'historique typé complet ; **les filtres et regroupements de l'Explorer se font dans le navigateur** (instantanés, sans aller-retour). Le serveur reste seul à décider du type, du côté compté d'une paire et du montant signé de chaque ligne : le client ne fait que filtrer et additionner des montants déjà signés. Un test d'API garantit que la somme du grand livre égale le Réel.
8. **Explorer vit dans Flux** : `Visé | Réel | Explorer`. Ses filtres sont **dans l'URL** (liens profonds depuis le Réel, bouton retour, partage d'un onglet) — intentionnel, contrairement à l'ancien `review` d'Opérations.
9. **Groupe de libellé** (« contrepartie ») = mots informatifs du libellé (`type_rules.telling_words` moins `patterns.label_common`), par sens, **tous comptes confondus** ; nom affiché = libellé nettoyé le plus fréquent du groupe. Pas de fusion manuelle.

## Global Constraints

- **Branche** : `feat/operation-categories` dans chaque repo, commits par-dessus.
- **Pas de migration** : ce qui est dérivé vit dans `bank_transfer_patterns` (contenu chiffré) → incrémenter `transfer_patterns._VERSION` à chaque changement de ce qui est dérivé.
- **Outillage de debug local** : `routes/dev_debug.py`, `scripts/debug_db.py`, `tests/routes/test_dev_debug.py` et le montage dans `main.py` ne sont **jamais** committés. Committer par chemins explicites. (`GZipMiddleware` de L2 touche aussi `main.py` : committer ce hunk seul, `git add -p`.)
- Tout chiffré ; aucune valeur jointable en clair avec `bank_transactions` dans une table portant `user_uuid_bidx`.
- Commentaires anglais, rares, le *pourquoi*. Conventional commits anglais, **3 lignes max**, aucune mention d'outil ou d'assistant.
- `uv run pytest -q` (sandbox désactivé) ; web : PATH node (`head -1 $(which pnpm)`) puis `pnpm type-check` et `pnpm test`. **Mutation sur chaque test neuf, après avoir vérifié que la base est verte.**
- Devises : on ne convertit pas. Montants en jeu, taux et agrégats dans la devise principale ; les autres devises à part, comme le Réel le fait déjà.
- Mode confidentialité : tout montant affiché passe par `maskValue`, y compris axes et infobulles des graphiques et le CSV (**non** masqué : c'est un export volontaire).
- Rejeu sur le dump : via `/dev/banking/study`, `scripts/debug_db.py` ou un script en scratchpad ; **lecture seule**, dump supprimé après usage.

---

# Partie Q — Questions : trier par montant, ignorer les petites, dire l'incertitude en euros

## Tâche Q1 — Montants en jeu et seuil dans la reconstruction

**Fichiers :** `services/banking/transfer_patterns.py`, `services/banking/flows.py` (`transfer_patterns`, bloc « groups », `_item_builder`), `dtos/banking.py`.

- [x] `TransferPatterns` : `flow_carriers` passe de `{uuid: count}` à `{uuid: {"count", "amount"}}` ; nouveaux `flow_open_amount: {période: montant}` et `questions_amount: {période: montant}` (paires `suggested`, sur le débit, comme `questions`). Lecture/écriture ; `_VERSION` incrémentée.
- [x] `FLOW_QUESTION_MIN_AMOUNT` dans `flows.py`, valeur fixée par Q4, commentaire avec la mesure. Un groupe sous le seuil n'entre ni dans `flow_carriers`, ni dans `flow_questions`, ni dans `flow_open*`.
- [x] `BankFlowQuestion` gagne `amount` (total du groupe).
- [x] Tests (mutation) : groupe à 49 € → aucune question, type par défaut, rien dans `flow_open` ; à 50 € → question ; groupe sous le seuil qui passe au-dessus après un import → question ; `amount` = somme du groupe ; paire `suggested` de 10 € → toujours sa question ; `questions_amount` compte la paire une fois.

## Tâche Q2 — Route de la file

**Fichiers :** `services/banking/flows.py`, `routes/banking.py`, `dtos/banking.py`.

- [x] `GET /banking/review-queue?year=` → `{total_amount, years: [{year, amount, count}], questions: [BankReviewItem]}`, `BankReviewItem = {transaction: BankTransactionItem, amount, kind: "flow" | "transfer"}`.
  - une entrée par porteuse de `flow_carriers`, une par paire `suggested` (jambe débit, montant de la paire) ;
  - `year` filtre sur l'année de l'opération porteuse ; `years` est toujours calculé sur tout l'historique (pour les puces) ;
  - **une seule passe sur l'historique** : charger, apparier, typer comme `_typed_history`, construire les items avec `_item_builder` sur les mouvements de tout l'historique (jamais `_transaction_item` par entrée) ;
  - tri : montant décroissant, puis date décroissante.
- [x] Non gatée, comme `/transactions`.
- [x] Tests (mutation) : ordre par montant ; une paire `suggested` apparaît une fois ; une question répondue disparaît ; groupe sous le seuil absent ; `total_amount` = somme des entrées ; `year` filtre les questions mais pas `years`.
- [x] Temps noté sur le dump (référence : `/banking/type-rules`, même passe, 151 ms).

## Tâche Q3 — Incertitude en euros dans le Réel

**Fichiers :** `services/banking/real_cashflow.py` (`_Reading.open_questions`), `dtos/banking.py`.

- [x] `RealCashflowMonth`, `RealCashflowYear`, `RealCashflowMonthDetail` gagnent `open_amount` = `questions_amount[p] + flow_open_amount[p]` (somme des mois pour l'année). `open_questions` conservé.
- [x] Tests (mutation) : mois avec une question de flux à 400 € et une paire `suggested` à 50 € → `open_amount` = 450 ; année = somme des mois ; question répondue → 0 ; groupe sous le seuil → 0.

## Tâche Q4 — Mesure du seuil (avant de figer la valeur)

- [x] Sur le dump, pour 50 et 100 € : questions restantes, euros laissés au défaut, part des euros en jeu. Retenir la valeur, la noter dans le commentaire de la constante et ici.
  **Retenu : 100 €.** À 100 € il reste 101 questions et 2 393 € (1,4 % des euros en jeu) passent au défaut ; à 50 €, 119 questions.

## Tâche Q5 — Onglet « À trier »

**Fichiers :** `src/types/index.ts`, `src/stores/cashflowTypes.ts`, nouvelle page `src/pages/BankReview.vue`, `src/router/index.ts`, `src/components/bank/BankTabs.vue`, `src/components/bank/BankTransactionRow.vue`.

- [x] Types `BankReviewItem`, `BankReviewQueue` ; `BankFlowQuestion.amount`.
- [x] Store : `fetchReviewQueue(year?)` ; toute écriture existante (`setType`, `answerFlow`, `clearOverride`, `deleteRule`, `decideTransfer`) garde `operationsRead()`, la page recharge sur `bank.dataRevision`.
- [x] Route `bank-review` (`/bank/review`), onglet « À trier » entre Comptes et Opérations, **le badge du total passe sur cet onglet**.
- [x] Page :
  - en-tête « **X € à confirmer** · N questions », et une barre « X € réglés depuis l'ouverture de la page » (rien n'est stocké : c'est la différence entre le premier `total_amount` chargé et l'actuel) ;
  - puces d'année (`years`, avec montant) + « Toutes » ;
  - liste de `BankTransactionRow` (réponses de flux et Oui/Non des paires déjà gérés) avec le montant en jeu et « N opérations » quand > 1 ; ligne retirée après réponse, avec « Annuler » quelques secondes (appelle `clearOverride` / `deleteRule`) ;
  - état vide « Tout est trié » et lien vers le Réel.
- [x] `BankTransactionRow` : afficher le montant en jeu dans la question quand il diffère du montant de la ligne.
- [x] Tests vitest (mutation) : le store appelle la route (avec et sans année) ; tri conservé tel que servi ; une réponse déclenche le rechargement.

## Tâche Q6 — Réel : euros à confirmer

**Fichiers :** `src/utils/realCashflow.ts` (`openQuestionsNotice`), `src/components/cashflow/RealCashflowOpenQuestions.vue`, `RealCashflowYear.vue`, `RealCashflowMonth.vue`, `src/pages/BankTransactions.vue`.

- [x] Bandeau : « X € restent à confirmer (N opérations) — ces chiffres peuvent encore changer », lien vers **À trier** filtré sur l'année affichée.
- [x] Retirer le paramètre `review` d'Opérations (`toReviewOnly` initialisé depuis la query, nettoyage dans le watcher).
- [x] `openQuestionsNotice(count, amount, currency)` ; tests (mutation) : 0 → null, singulier/pluriel, montant formaté.

# Partie C — Couverture et fraîcheur des comptes

## Tâche C1 — API

**Fichiers :** `services/banking/real_cashflow.py`, `dtos/banking.py`.

- [x] `RealCashflowYear` et `RealCashflowMonthDetail` gagnent `coverage: [{account_id, account_name, account_type, first_day, last_day, synced: bool}]` pour les comptes lisibles. Les dates étant chiffrées, aucun `min/max` SQL n'est possible : `first_day` / `last_day` par compte sont calculés dans la reconstruction des patterns (qui lit déjà tout l'historique) et stockés (`coverage`, `_VERSION` incrémentée).
- [x] `gaps` calculé côté API : pour la période affichée, les comptes dont `first_day` est après le début ou `last_day` avant la fin (mois terminé), avec le nombre de jours manquants.
- [x] Tests (mutation) : compte commencé en mars → signalé sur l'année, pas sur un mois d'avril ; compte arrêté au 31/08 → signalé sur septembre seulement.

## Tâche C2 — Web

**Fichiers :** nouveau `src/components/cashflow/RealCashflowCoverage.vue`, `RealCashflowYear.vue`, `RealCashflowMonth.vue`.

- [x] Sous le bandeau d'incertitude, une ligne discrète : « Epargne (Livret A) n'a d'opérations qu'à partir du 28 janv. 2025 : les virements vers lui avant cette date comptent en dépenses. » ; « Epargne à jour au 31 août » + lien Importer.
- [x] Tests vitest (mutation) : pas de trou → rien ; trou de début / de fin → message ; pluriel.

# Partie L — Grand livre

## Tâche L1 — `GET /banking/ledger`

**Fichiers :** `services/banking/flows.py` (ou nouveau `services/banking/ledger.py` qui s'appuie sur `_typed_history`), nouveau `services/banking/label_groups.py`, `routes/banking.py`, `dtos/banking.py`.

- [x] Réponse `BankLedger` :
  - `currency` (principale), `digest` (= `source_digest` des patterns) ;
  - `accounts: [{id, name, type, institution, first_day, last_day, synced, balance}]` ;
  - `groups: [{key, name, is_credit}]` (dédoublonnés, référencés par index depuis les lignes) ;
  - `rows: [BankLedgerRow]` triées par date décroissante :
    `id, account (index), day, amount (non signé), currency, is_credit, is_pending, label, group (index), operation_type, cashflow_type, type_source, counted (bool), signed (montant signé dans son type, 0 si non compté), transfer_status, question: "flow" | "transfer" | null, open (bool : son groupe attend une réponse)`.
- [x] `counted` et `signed` viennent **exactement** de `counted_leg` et `signed_amount` : ce sont eux que le client additionne. Une ligne en attente (`is_pending`) a `counted = false` (le Réel l'exclut) mais reste listée.
- [x] `label_groups.py` (pur) :
  - `group_key(label, is_credit, common)` = mots de `type_rules.telling_words(label)` moins `common` (union des `label_common` des comptes du sens), triés, joints ; libellé sans mot informatif → clé = signature ;
  - `display_label(label)` : libellé pour l'affichage, **jamais lu par un total** (même statut que `operation_types.py`, docstring qui le dit) : retire le préfixe date carte (`CARTE jj/mm/aa`), `CB*nnnn`, `VIR SEPA` / `VIR INST` / `PRLV SEPA`, références `Réf : …`, espaces multiples ; casse titre si tout en majuscules ;
  - nom d'un groupe = `display_label` le plus fréquent de ses lignes finales, à égalité le plus récent.
- [x] Cache HTTP : `ETag: "<digest>"` ; `If-None-Match` égal → 304 sans relire l'historique.
- [x] Non gatée.
- [x] **Test de parité (mutation)** : sur un jeu de données couvrant paire épargne, paire `recurring`, `refund`, règle `SAVING`, remboursement, ligne en attente et devise étrangère : pour chaque mois terminé et chaque type, somme des `signed` des lignes `counted` en devise principale = `real_cashflow_year(...).months[p]` ; nombre de lignes `counted` = `operation_count`.
- [x] Tests purs `label_groups` (mutation) : `CARTE 21/06/26 CARREFOUR ANNECY CB*0837` → « Carrefour Annecy » ; `VIR SEPA TRANSALP'DOME S.A.S.` → « Transalp'dome S.A.S. » ; `Vinted` inchangé ; deux références de salaire VILMORIN différentes → même clé ; un mot commun du côté ignoré dans la clé.
- [x] Temps et taille mesurés sur le dump, notés ici (attendu ≈ `type-rules`, 151 ms ; 942 Ko).

## Tâche L2 — Compression

**Fichiers :** `main.py` (hunk seul), `tests/…` si un test de middleware existe.

- [x] `GZipMiddleware(minimum_size=1024)`. Vérifier dans `capitalview-infra/deploy` si le proxy de prod compresse déjà ; si oui, le noter et garder le middleware quand même (dev et proxy sans compression).
- [x] Test : `/banking/ledger` avec `Accept-Encoding: gzip` → `Content-Encoding: gzip`.

## Tâche L3 — Store web

**Fichiers :** `src/types/index.ts`, nouveau `src/stores/ledger.ts`, `src/services/sessionReset.ts`.

- [x] `fetchLedger(force?)` : `If-None-Match` avec le dernier `digest` (le client API expose-t-il les en-têtes ? sinon ajouter une variante `getWithEtag` à `api/client.ts`, testée) ; rechargement sur `bank.dataRevision`.
- [x] Lignes « hydratées » une fois (compte, groupe par index → objets ; `day` → `Date` ; montants → `number`).
- [x] Remis à zéro dans `sessionReset`.
- [x] Tests vitest (mutation) : 304 garde les données ; `dataRevision` recharge ; hydratation des index.

# Partie R — Réel enrichi

Tout ce qui suit est calculé **côté API** dans `real_cashflow.py` (le Réel reste la source des chiffres de synthèse) et affiché dans `RealCashflowYear.vue` / `RealCashflowMonth.vue`, sauf R4 (grand livre).

## Tâche R1 — Taux d'épargne

- [x] `RealCashflowTotals` gagne `savings_rate` et `placed_rate` (`Decimal | None`, en %), calculés dans `_totals` et `_sum` ; `monthly_mean` / `monthly_median` : taux recalculé depuis les montants moyens/médians, **pas** moyenne des taux.
- [x] Web : 6ᵉ carte « Taux d'épargne 34 % · dont placé 21 % », couleur par seuil (< 0 danger, < 10 % warning, sinon success) ; barres « Mois par mois » : ligne du taux sur un second axe.
- [x] Tests (mutation) : entrées 0 → `null` ; entrées 2 000, dépenses 1 500, épargne 200 → 25 % et 10 % ; médiane de l'année = taux des médianes.

## Tâche R2 — Matelas de sécurité

- [x] `RealCashflowYear.safety_net` (seulement pour l'année en cours) : `liquid` = soldes des comptes courants + livrets (`SAVINGS_ACCOUNTS`) de l'app ; `monthly_expenses` = médiane des dépenses des 12 derniers mois terminés ; `months` = `liquid / monthly_expenses` ; `savings_only_months` = livrets seuls ; `stale_accounts` = comptes non synchronisés depuis > 7 jours (leur solde peut dater).
- [x] Web : carte « Matelas : 1,3 mois de dépenses » (3 451 € disponibles / 2 514 € par mois), sous-ligne « dont livrets 1,2 mois », avertissement si `stale_accounts`.
- [x] Tests (mutation) : aucune dépense → `null` ; compte d'investissement exclu ; compte périmé listé.

## Tâche R3 — Cette année contre l'an dernier

- [x] `RealCashflowYear.previous_year_to_date` : totaux de l'année précédente **sur les mêmes mois** que les mois terminés affichés (pour une année passée : l'année entière) ; `null` si l'année précédente n'a aucun mois couvert.
- [x] `RealCashflowYear.projection` (année en cours seulement) : totaux de l'année + médiane mensuelle × mois restants, par type.
- [x] `RealCashflowMonth.atypical: bool` : dépenses du mois > médiane + 1,5 × écart interquartile des mois couverts de l'année (≥ 6 mois couverts, sinon toujours `false`).
- [x] Web : sous chaque carte « +12 % vs 2025 à date » (flèche, couleur selon le sens souhaitable du type) ; carte « Fin d'année estimée » repliable ; mois atypique marqué dans le graphique (barre contournée + infobulle « mois inhabituel »).
- [x] Tests (mutation) : 8 mois terminés → comparaison sur janvier–août N-1 ; année passée → année entière ; moins de 6 mois → aucun mois atypique ; projection = total + médiane × restant.

## Tâche R4 — Mois en cours : le rythme

**Fichiers :** `services/banking/real_cashflow.py` (ou `current_month.py`), `routes/banking.py`, `dtos/banking.py`, nouveau `src/components/cashflow/RealCashflowPace.vue`.

- [x] `GET /banking/real-cashflow/current` → `{period, day, spent_to_date, pending_to_date, median_to_date, median_month, curve: [{day, spent, median}], projection, open_amount}` :
  - dépenses du mois en cours **finales et en attente** (le rythme du jour compte ce qui est passé en carte), types résolus comme le Réel ;
  - médiane, jour par jour, des dépenses cumulées des 12 derniers mois terminés (jour J d'un mois plus court = son dernier jour) ;
  - projection = `spent_to_date + (median_month − median_to_date)`.
- [x] Web : en tête de la vue Réel, au-dessus du sélecteur d'année, une carte compacte « Septembre : 1 073 € dépensés au 16 · médiane au 16 : 790 € · fin de mois estimée 1 837 € » + courbe cumulée (mois en cours contre médiane, zone ombrée) ; lien « Explorer ce mois ».
- [x] Tests (mutation) : médiane au jour 31 sur un mois de 30 jours ; ligne en attente comptée ; épargne jamais comptée en dépense ; mois sans historique → `median_to_date = null` et carte sans comparaison.

## Tâche R5 — D'où vient, où va l'argent

- [x] `RealCashflowYear` et `RealCashflowMonthDetail` gagnent `top_sources` et `top_destinations` : les 5 groupes de libellé (L1) les plus lourds parmi les lignes comptées `INCOME` (crédits) et `EXPENSE` (débits), avec `{group_key, name, amount, operation_count, share}` ; **à la place** de `top_expenses` côté écran, mais `top_expenses` reste servi (Opérations individuelles, onglet de la carte).
- [x] Web : carte à deux onglets « Où va l'argent » / « D'où vient l'argent » ; barre de part horizontale par ligne ; clic → Explorer filtré sur le groupe et l'année. Onglet « Plus grosses opérations » = ancien `top_expenses`.
- [x] Sankey réel (réutiliser `CashflowSankeyChart.vue` si son contrat le permet, sinon le généraliser) : 4 premières sources + « Autres entrées » → Dépenses / Épargne / Investissement / Reste ; nœud « Reste » négatif affiché comme « Pris sur l'existant ».
- [x] Tests (mutation) : un remboursement (`EXPENSE` crédit) ne crée pas de source ; deux libellés VILMORIN → une seule source ; `share` sur le total du type.

# Partie X — Page Explorer

## Tâche X1 — Moteur de filtres et d'agrégats (pur)

**Fichiers :** nouveau `src/utils/ledger.ts` et `src/utils/__tests__/ledger.spec.ts`.

- [x] `LedgerFilters` :
  - `period` : `{preset: 'month' | 'last-month' | '3m' | '12m' | 'ytd' | 'last-year' | 'all' | 'custom', from?: 'YYYY-MM', to?: 'YYYY-MM'}` (mois **complets** ; `month` = mois en cours) ;
  - `accounts: string[]`, `direction: 'all' | 'in' | 'out'`, `types: CashflowType[]`, `means: OperationType[]`, `min?/max?: number` (montant absolu), `query: string` (mots séparés par des espaces, tous requis, `-mot` exclut, sur `label` et nom du groupe, accents et casse ignorés), `groups: string[]` (clés), `onlyOpen: boolean`, `includePending: boolean` (défaut `false`), `includeUncounted: boolean` (paires et neutres non comptés ; défaut `false`).
- [x] `applyFilters(rows, filters, today)` ; `summarize(rows)` → `{total (somme de signed par type), count, perMonth (moyenne sur les mois de la période), median, averageTicket, openAmount}` ;
- [x] `groupBy(rows, dimension)` avec `dimension ∈ 'group' | 'month' | 'week' | 'account' | 'means' | 'type' | 'weekday' | 'amount-band'` → `[{key, label, total, count, share, averageTicket, lastDay, spark: number[12]}]`, trié par total absolu ;
- [x] `timeSeries(rows, granularity, stackBy: 'type' | 'group-top5')` (granularité automatique : période ≤ 3 mois → semaine, sinon mois) ;
- [x] `comparePeriods(rows, filters, today)` → mêmes filtres sur la période précédente de même durée **et** sur la même période N-1 : `{previous, lastYear}` avec `delta` et `deltaPct` ;
- [x] `toCsv(rows)` : date, compte, libellé, montant signé bancaire, devise, type, moyen de paiement, groupe ; séparateur `;`, décimale `,` (Excel FR), BOM UTF-8.
- [x] `serializeFilters` / `parseFilters` ↔ query string (valeurs inconnues ignorées, pas d'erreur).
- [x] Tests vitest (mutation sur chacun) : période `12m` exclut le mois en cours ; `-uber` exclut ; accents ignorés ; min/max sur montant absolu ; `includeUncounted=false` exclut une paire `recurring` ; `share` somme à 100 ; `comparePeriods` N-1 sur une année bissextile ; `spark` de 12 mois avec zéros ; aller-retour `serializeFilters`/`parseFilters` ; CSV avec virgule décimale et libellé contenant `;`.

## Tâche X2 — Page

**Fichiers :** `src/pages/Cashflow.vue` (3ᵉ segment `Explorer`, `CashflowView` gagne `'explore'`), nouveau `src/components/cashflow/explore/` : `ExploreView.vue`, `ExploreFilters.vue`, `ExploreSummary.vue`, `ExploreChart.vue`, `ExploreBreakdown.vue`, `ExploreOperations.vue` ; nouveau `src/composables/useExploreFilters.ts` (filtres ↔ URL).

Mise en page (desktop, de haut en bas) :

1. **Barre de filtres collante**
   - Période : segmented control des préréglages + « Personnalisé » (deux sélecteurs de mois) ; flèches ◀ ▶ qui décalent la période de sa durée.
   - Recherche libellé (avec l'aide « plusieurs mots, -mot pour exclure »).
   - Puces multi-sélection : Comptes · Types (couleurs des types) · Moyens de paiement ; sélecteur Sens ; montant min / max.
   - Interrupteurs : « Seulement à confirmer », « Inclure en attente », « Inclure virements internes ».
   - Filtres actifs rappelés en puces supprimables + « Tout effacer ».
   - **Mobile** : bouton « Filtres (3) » qui ouvre un tiroir plein écran, recherche et période restent visibles.
2. **Résumé de la sélection** (4 tuiles) : Total · Opérations (panier moyen, médiane) · Par mois · Comparaison (« −8 % vs période précédente · +15 % vs 2025 »). Si la sélection porte des euros à confirmer : « dont 420 € à confirmer » avec lien À trier.
3. **Graphique temporel** (ECharts) : barres empilées par type, ou par les 5 premiers groupes + « Autres » (bascule) ; ligne de moyenne ; bascule **« Cumulé »** qui superpose la période et la même période N-1 en courbes cumulées ; cliquer une barre restreint la période à ce mois ; brosser une plage la restreint.
4. **Répartition** : sélecteur « Regrouper par » (Contrepartie, Mois, Semaine, Compte, Moyen de paiement, Type, Jour de semaine, Tranche de montant) ; tableau : nom, barre de part, total, nombre, panier moyen, dernière opération, sparkline 12 mois, variation vs période précédente ; tri par colonne ; cliquer une ligne **ajoute le filtre** (fil d'Ariane « Tout › Dépenses › Carrefour Annecy ») ; mobile : cartes au lieu du tableau.
5. **Opérations** de la sélection : réutilise `BankTransactionRow` (picker de type et questions inclus : corriger ici met à jour tout l'écran via `dataRevision`), groupées par jour, tri date / montant, pagination par 50 (« Afficher plus »).
6. **Actions** : « Exporter en CSV » (sélection), « Copier le lien » (URL avec filtres).
7. **Vues prêtes** en tête (puces, simples raccourcis de filtres) : « Où va l'argent (12 mois) », « D'où vient l'argent (12 mois) », « Gros achats > 200 € », « Espèces » (moyen de paiement retrait), « À confirmer ». Pas de vue qui suppose des commerçants précis : elle ne vaudrait que pour un utilisateur.

- [x] Liens profonds depuis le Réel : carte de type → Explorer (type, année) ; barre de mois → reste le détail du mois, qui gagne « Explorer ce mois » ; ligne « Où va l'argent » → Explorer (groupe, année) ; carte du rythme → Explorer (mois en cours, dépenses, en attente incluses).
- [x] Chargement : squelettes ; aucune opération → état vide existant du Réel ; erreur → `BaseAlert` + Réessayer.
- [ ] Performance : filtres et agrégats en `computed` sur les lignes hydratées, recherche avec un `debounce` de 150 ms ; mesurer dans Chrome sur le dump (objectif : < 50 ms par changement de filtre, noté ici).
- [x] Accessibilité : graphiques doublés par le tableau de répartition ; toutes les puces focusables ; contraste des couleurs de type vérifié en sombre.
- [x] Tests vitest (mutation) : `useExploreFilters` lit et écrit l'URL sans empiler l'historique à chaque frappe (`router.replace`) ; cliquer une ligne de répartition ajoute le filtre ; « Tout effacer » vide l'URL ; la vue Explorer est mémorisée comme Visé / Réel.

## Tâche X3 — Contrôle visuel

- [ ] Chrome desktop (1440) puis 390 px, clair puis sombre, mode confidentialité : chaque préréglage de période, recherche « carrefour », regroupement par contrepartie puis clic, cumulé N-1, export CSV ouvert dans un tableur, correction d'un type depuis la liste (puis annulée). Captures jointes au compte rendu.

## Vérification finale

1. `uv run pytest -q`, `pnpm type-check`, `pnpm test` verts ; mutation faite sur chaque test neuf ; nombre de tests noté.
2. Lecture qui force la reconstruction des patterns (`_VERSION`), puis sur le dump : questions avant/après seuil, `total_amount` de la file, temps de `/banking/review-queue`, `/banking/real-cashflow`, `/banking/real-cashflow/current`, `/banking/ledger` (et 304), taille gzip du grand livre.
3. Test de parité grand livre ↔ Réel vert, et contrôle manuel sur 2025 : totaux de l'Explorer (année 2025, types) = cartes du Réel 2025.
4. Chrome (X3), plus À trier (ordre, réponse qui retire la ligne, badge), Réel (bandeaux, couverture, taux, matelas, rythme, sources), Opérations (plus de `review` dans l'URL). Toute réponse de test annulée ensuite, base vérifiée sans règle ni dérogation résiduelle.
5. `git status` : `main.py` committé **seulement** pour le hunk `GZipMiddleware`, jamais avec le montage du routeur de debug ; outillage de debug jamais suivi.

## Après ce plan (ordre recommandé, à planifier séparément)

1. **Abonnements** (lot 2 du plan parent) : « dont abonnements », charges fixes, reste à vivre, hausses de prix ; l'Explorer gagne le filtre et la répartition « Abonnement ». Validation sur le dump comme prévu.
2. **Visé ↔ réel par ligne déclarée** : rattacher chaque ligne du visé (15 aujourd'hui) à un ou plusieurs groupes de libellé ; écran « Loyer : prévu 530 €, payé 530 € », « Papa : prévu 690 €, reçu 612 € en moyenne » ; ce qui n'est rattaché à rien = variable. Pas une catégorisation : une réponse par ligne visée.
3. **Soldes entre proches** : groupes de libellé qui ont des débits **et** des crédits vers la même personne (ex. Titouan Rattin) → « vous a remboursé 1 106 €, vous lui avez envoyé X € ». À mesurer d'abord : le rapprochement d'un nom entre « Virement à : » et « Virement de : » n'est pas garanti.
4. **Alertes** : mois qui dépasse la médiane au même jour, abonnement qui augmente, solde courant qui passe sous un plancher (l'historique quotidien des soldes existe : 9 jours sous 100 € sur 12 mois).

## Hors périmètre

Catégories de dépenses sous quelque forme que ce soit, IA, rapprochement avec les versements des comptes d'investissement de l'app, fusion manuelle de commerçants, frais bancaires, doublons, fixe/variable par moyen de paiement, pockets Revolut.
