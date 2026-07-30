# Analyse comportementale — Jalon M2 : le pont contrefactuel et le coût d'exécution

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Contexte

M1 a livré une seule métrique en ligne — l'écart investisseur MWR−TWR — plus toute la plomberie :
`reliability.py`, `flows.py`, `returns.py`, `benchmark.py`, `report.py`, l'endpoint
`GET /analytics/investor`, la page `/analyse` et son store. Tout est sur `feat/investor-analytics`
dans les deux dépôts.

M2 livre les deux métriques restantes du Tier 1 de la spec (`§1.2` et `§1.3`) : le **pont
contrefactuel** (combien le comportement a coûté par rapport à un robot qui achèterait
mécaniquement) et le **coût d'exécution** (le prix payé par ordre comparé au prix moyen de son
mois, avec test de permutation). Ce sont les deux blocs qui transforment « voici ton écart » en
« voici d'où il vient et combien il vaut en euros ».

**Le problème structurant de ce jalon, et la raison pour laquelle il ne ressemble pas à M1 :**
M1 ne lisait qu'**une** série de prix (le benchmark) et se reposait sur les snapshots
`AccountHistory` déjà en base. M2 doit rejouer le portefeuille **actif par actif, jour par jour,
sur toute la profondeur d'historique de l'utilisateur** — donc récupérer et aligner les prix de
chaque ligne détenue, plus les taux de change, sur une fenêtre qui n'est pas connue à l'avance :
elle dépend entièrement des données de l'utilisateur. Un utilisateur avec 3 ans d'historique a
besoin de 3 ans de cours et de 3 ans d'EUR/USD. Rien ne doit être codé en dur.

**Contrainte oubliée qui casse tout si on la découvre tard :** le benchmark par défaut est un ETF,
et un ETF a une date de lancement. Si l'utilisateur a 3 ans d'historique et que l'ETF de référence
en a 18 mois, le contrefactuel est **inconstructible** sur la première moitié de la fenêtre. M2
doit détecter cette couverture et le dire, jamais produire un chiffre sur une base tronquée en
silence. C'est aussi ce qui tranche la question de l'ISIN par défaut (voir Décisions).

**Spec :** `capitalview-api/docs/superpowers/specs/2026-07-29-investor-behaviour-analytics-design.md`
(§1.2, §1.3, §2 pour le cadre de fiabilité, §8 pour l'architecture, §11 pour les garanties de test)

---

## Décisions actées avec l'utilisateur

| Sujet | Décision |
|---|---|
| ISIN benchmark par défaut | **`IE00B4L5Y983` inchangé** (iShares Core MSCI World UCITS ETF USD Acc, coté depuis 2009). C'est la profondeur d'historique qui décide, pas la marque : une part lancée récemment casserait le contrefactuel sur les portefeuilles anciens. |
| Sélecteur de benchmark | **Dans M2.** La colonne `benchmark_asset_key` existe depuis M1 mais aucune UI ne l'expose — elle est morte tant que M2 ne la branche pas. |
| numpy | **Ajouté explicitement** à `pyproject.toml` dans ce jalon (il n'est aujourd'hui présent que transitivement ; `uv.lock` porte déjà 2.4.2). Il sert aux 5 000 permutations et resservira à l'ACP en M3. |

**Détection de couverture du benchmark :** incluse malgré le choix de garder `IE00B4L5Y983`. Elle
ne dépend pas de l'ISIN retenu — c'est la garantie qui rend le sélecteur utilisable sans risque de
chiffre faux, puisque l'utilisateur pourra y mettre n'importe quel ETF.

---

## Global Constraints

- **Deux dépôts git distincts**, même branche `feat/investor-analytics` dans les deux. Ne jamais
  lancer deux agents implémenteurs en parallèle sur le même repo ; un agent API + un agent web en
  parallèle sont sans risque.
- **Commits** : conventional commits en anglais, scope quand il est évident, 2-3 lignes maximum,
  pas de liste à puces, **pas de trailer, pas de co-author**.
- **Code, commentaires, docstrings et noms de fichiers en anglais.** Commentaires peu nombreux,
  uniquement là où le *pourquoi* n'est pas évident.
- **Ne rien casser** : `Stock.vue`, les services stock existants et les 8 encarts ne sont pas
  modifiés. La suite existante doit passer inchangée (616 tests au départ de M2).
- **Argent** : `Decimal` côté stockage et API. numpy travaille en `float64` en interne et
  reconvertit en `Decimal` en sortie — à documenter dans les docstrings concernées, comme
  `returns.py` le fait déjà pour le solveur XIRR.
- **Migrations Alembic** : générées par `alembic revision --autogenerate`, **jamais d'ID écrit à la
  main** (cf. section « Database migrations » du README, ajoutée après l'incident de M1). M2 ne
  devrait avoir besoin d'aucune migration.
- **Pas de nouvelle dépendance frontend** : ECharts 6 + `vue-echarts` sont déjà là, et couvrent le
  waterfall comme le box plot.

### Piège d'environnement — à traiter avant la première tâche

Le conteneur de développement n'a que **Python 3.14.0rc2**, alors que pydantic 2.12.5 appelle
`typing._eval_type(..., prefer_fwd_module=True)` — paramètre qui n'existe qu'à partir de 3.14
final. Sans correctif, **toute la suite backend échoue au collect**, ce qui ressemble à s'y
méprendre à un bug du code. `uv python install 3.14` ne propose que rc2.

Contournement, à réappliquer dans le `.venv` (non versionné) après chaque `uv sync` : dans
`pydantic/_internal/_typing_extra.py::_eval_type`, ne passer `prefer_fwd_module` que si
`inspect.signature(typing._eval_type)` l'accepte.

Les tests ont aussi besoin des variables d'environnement de `.github/workflows/ci.yml`
(`SECRET_KEY`, `DATABASE_URL`, `ENCRYPTION_KEY`, `COMMUNITY_ENCRYPTION_KEY`, `CMC_API_KEY`,
`CG_API_KEY`).

---

## Structure des fichiers

| Fichier | Responsabilité |
|---|---|
| `capitalview-api/services/analytics/prices.py` | **R2** — matrice de prix journalière multi-actifs, forward-fill, source unique partagée avec `account_history.py`. |
| `capitalview-api/services/analytics/window.py` | Résolution de la fenêtre d'analyse à partir des données de l'utilisateur + couverture réelle du benchmark. |
| `capitalview-api/services/analytics/execution.py` | TWAP mensuel, slippage par ordre en bps, agrégat pondéré, distribution. |
| `capitalview-api/services/analytics/timing.py` | Moteur de permutation mutualisé (numpy). Resservira en M3 pour §2.2. |
| `capitalview-api/services/analytics/counterfactual.py` | Rejeu robot + substitutions séquentielles, réconciliation exacte. |
| `capitalview-api/services/analytics/report.py` | *(modifié)* assemblage des nouveaux blocs, gates, verdicts. |
| `capitalview-api/dtos/analytics.py` | *(modifié)* DTOs des deux nouveaux blocs. |
| `capitalview-web/src/components/analytics/AttributionWaterfall.vue` | Le pont contrefactuel (ECharts). |
| `capitalview-web/src/components/analytics/SlippageDistribution.vue` | Box plot + histogramme du slippage. |
| `capitalview-web/src/components/analytics/BenchmarkPicker.vue` | Sélecteur de benchmark. |
| `capitalview-web/src/pages/Analysis.vue` | *(modifié)* les deux nouveaux blocs + le sélecteur. |

---

## Task 1 · R2 — la matrice de prix comme source unique

**Pourquoi.** `account_history.py` possède déjà exactement ce dont M2 a besoin :
`_get_price_matrix` (`services/account_history.py:235`) et `_fill_price_gaps` (`:267`), qui
construisent `{asset_key: {date: price}}` en une seule requête JOIN puis comblent les trous par
forward-fill avec un seed SQL pour les actifs sans cotation avant la fenêtre. Les réécrire dans
`analytics/` créerait une deuxième définition du prix journalier — exactement l'erreur que R1 a
évitée pour les flux en M1.

**Files:** Create `services/analytics/prices.py` · Modify `services/account_history.py` ·
Test `tests/services/analytics/test_prices.py`

**Produces:**
- `get_price_matrix(session, asset_keys, from_date, to_date) -> dict[str, dict[date, Decimal]]`
- `fill_price_gaps(session, matrix, asset_keys, days) -> dict[str, dict[date, Decimal]]`
- `daily_series(matrix, asset_key, days) -> list[Decimal | None]`

- [ ] Déplacer les deux fonctions **sans changer une ligne de leur corps**, ré-exporter depuis
      `account_history.py` (`from services.analytics.prices import ...`) et supprimer les
      définitions privées.
- [ ] Les tests existants `tests/services/test_account_history.py` et `test_history_services.py`
      doivent passer **inchangés** — c'est le contrat de non-régression du refactor.
- [ ] Ajouter les tests de `prices.py` : matrice creuse, forward-fill, seed depuis une cotation
      antérieure à la fenêtre, actif totalement inconnu.
- [ ] **Duplication trouvée en revue de plan, à corriger dans cette même tâche :**
      `services/analytics/benchmark.py::get_benchmark_series` (écrit en M1) réimplémente son propre
      forward-fill au lieu d'utiliser `_get_price_matrix`/`_fill_price_gaps`, et **sans** leur filet
      de sécurité : si la première date de la fenêtre n'a pas de cotation, ces jours restent vides
      au lieu d'être amorcés depuis la dernière cotation antérieure. Rebrancher
      `get_benchmark_series` sur `get_price_matrix` + `fill_price_gaps` (un seul actif dans la
      liste), pour qu'il ne reste plus qu'une définition du forward-fill dans le repo. Ajouter un
      test qui couvre précisément ce cas : fenêtre démarrant un jour non coté, avec une cotation
      antérieure disponible en base — doit être comblée, pas laissée vide.
- [ ] `uv run pytest -q` — même nombre de succès qu'avant la tâche, plus les nouveaux tests.

---

## Task 2 · La fenêtre d'analyse — dérivée de l'utilisateur, jamais codée en dur

**Pourquoi.** C'est le cœur du problème signalé. Le pattern correct existe déjà dans
`run_lazy_catchup` (`services/account_history.py:980-1046`) : il calcule
`earliest_date = min(...)` **à partir des données du compte**, puis appelle
`ensure_price_history(session, asset_key, atype, earliest_date)` pour chaque actif et
`get_historical_exchange_rates_db(session, currency, earliest_date, yesterday)` pour chaque devise.
M2 réutilise ces deux helpers de `services/market.py` — il n'écrit aucun accès réseau.

**Files:** Create `services/analytics/window.py` · Test `tests/services/analytics/test_window.py`

**Produces:** `AnalysisWindow` (dataclass frozen) et
`resolve_window(session, transactions, benchmark_key) -> AnalysisWindow`

```
AnalysisWindow:
    start: date              # premier BUY de l'utilisateur (pas premier dépôt — cf. §0 bis)
    end: date                # hier (dernier jour avec cotation garantie)
    days: int
    asset_keys: list[str]    # toutes les lignes jamais détenues, EUR exclu
    benchmark_key: str
    benchmark_from: date | None   # première cotation réellement disponible du benchmark
    benchmark_covers_window: bool
    clamped_start: date | None    # start effectif si le benchmark ne couvre pas tout
```

- [ ] `start` est la date du **premier `BUY`**, pas du premier dépôt : le contrefactuel compare des
      décisions d'investissement (§0 bis de la spec). Le cash drag (terme *d*) réintroduit les
      dépôts, il est le seul à le faire.
- [ ] Appeler `ensure_price_history` pour chaque `asset_key` détenu **et** pour le benchmark, avec
      `from_date=start` — donc la profondeur exacte de l'utilisateur, ni plus ni moins.
- [ ] Appeler `get_historical_exchange_rates_db` pour chaque devise non-EUR rencontrée, sur
      `[start, end]`. Note : `_backfill_stock_prices` (`services/market.py:946-964`) convertit déjà
      les cours en EUR via des taux historiques par date, donc `market_price_history.price` est en
      EUR et le rejeu n'a pas de conversion à refaire. Les taux ne servent qu'aux actifs dont la
      devise de cotation diffère et qui ne passeraient pas par ce chemin.
- [ ] **Couverture du benchmark** : lire sa première cotation en base après backfill. Si elle est
      postérieure à `start`, poser `benchmark_covers_window=False` et `clamped_start` à cette date.
- [ ] Tests : historique de 3 ans → fenêtre de 3 ans ; benchmark lancé au milieu → `clamped_start`
      posé et `benchmark_covers_window=False` ; aucun `BUY` → fenêtre nulle proprement gérée ;
      vérifier par mock que `ensure_price_history` reçoit bien `start` et pas une constante.

---

## Task 3 · numpy et le moteur de permutation mutualisé

**Files:** Modify `pyproject.toml` · Create `services/analytics/timing.py` ·
Test `tests/services/analytics/test_timing.py`

**Produces:** `permutation_test(observed, null_samples) -> PermutationResult`
(`p_value`, `percentile`, `n_draws`) et `draw_null_distribution(...)`.

- [ ] Ajouter `"numpy>=2.4"` aux `dependencies` de `pyproject.toml`, puis `uv lock` (la version
      2.4.2 est déjà résolue dans `uv.lock`, la lock ne devrait pas bouger).
- [ ] Le moteur est **générique** : il reçoit une statistique observée et un tableau de tirages
      nuls, il ne connaît rien au slippage. C'est ce qui permettra à M3 de le réutiliser pour §2.2
      sans le réécrire.
- [ ] Seed RNG **fixe et documenté** (`np.random.default_rng(0)`) : sans ça, deux chargements de la
      page donnent deux p-values différentes, et l'utilisateur perd confiance à juste titre.
- [ ] **Test de calibration exigé par §11 de la spec** : sur des données synthétiques à biais nul,
      la p-value doit être approximativement uniforme sur [0,1]. Vérifier sur 200 jeux synthétiques
      que la proportion de `p < 0.05` reste dans un intervalle raisonnable (bornes larges, ce test
      ne doit pas flapper).

---

## Task 4 · Coût d'exécution — slippage vs TWAP mensuel

**Files:** Create `services/analytics/execution.py` ·
Test `tests/services/analytics/test_execution.py`

**Produces:** `ExecutionAnalysis` et
`analyse_execution(buys, price_matrix, rng) -> ExecutionAnalysis`

**Algorithme.** Pour chaque `BUY` d'un actif A le jour d :
- `TWAP = moyenne des clôtures journalières de A sur le mois calendaire de d`, calculée uniquement
  sur les jours **réellement cotés** (présents dans la matrice avant forward-fill).
- `slippage_bps = (prix_payé − TWAP) / TWAP × 10 000`.
- Agrégat pondéré par le notionnel EUR : `Σ(wᵢ·slippageᵢ) / Σwᵢ` avec `wᵢ = amountᵢ × price_per_unitᵢ`.
- Distribution complète (min, Q1, médiane, Q3, max) pour le box plot.

- [ ] **Nommage honnête (§1.3)** : c'est un **TWAP sur clôtures journalières**, pas un VWAP. Le mot
      VWAP ne doit apparaître nulle part, ni dans le code ni dans l'UI.
- [ ] `price_per_unit` est **déjà en EUR** pour les comptes titres — tout le service traite
      `amount × price_per_unit` comme un coût EUR (`services/stock_transaction.py:216-220, 310`) —
      et `market_price_history.price` aussi. Aucune conversion, mais l'écrire en commentaire : c'est
      exactement le genre d'hypothèse qui se casse en silence.
- [ ] **Permutation** : chaque achat est re-daté uniformément parmi les jours de bourse de **son
      propre mois**, montant et actif constants, 5 000 tirages, prix rejoué = clôture du jour tiré.
      Les jours de bourse viennent de `get_non_trading_days` (`services/market.py:67`, adossé à
      `exchange_calendars`, déjà dépendance directe) avec le MIC de `MarketAsset.exchange` ;
      repli sur les jours présents dans la matrice si le MIC est inconnu.
- [ ] Gate : `insuffisant` sous 10 achats, `indicatif` sous 30, `solide` au-delà. Si
      `p > 0.10`, le verdict devient « rien de détectable » — **jamais** « tu es bon ».
- [ ] Tests : slippage nul quand le prix payé = TWAP ; signe correct sur un achat au plus haut du
      mois ; pondération (un gros ordre pèse plus qu'un petit) ; mois à une seule cotation ;
      p-value élevée sur des achats aléatoires.

---

## Task 5 · Le pont contrefactuel

**Files:** Create `services/analytics/counterfactual.py` ·
Test `tests/services/analytics/test_counterfactual.py`

**Produces:** `Bridge` (liste de `BridgeStep` + `residual`) et
`build_bridge(window, transactions, price_matrix, benchmark_series) -> Bridge`

**Baseline « robot »** : le même capital total effectivement investi, réparti en **achats mensuels
égaux** entre le premier et le dernier achat, sur le benchmark, zéro frais, zéro cash dormant.

**Substitutions séquentielles**, chaque terme valorisé à `window.end` :

| Étape | On remplace | Terme |
|---|---|---|
| V0 | robot : mensualités égales sur le benchmark | baseline |
| V1 | ton calendrier et tes montants réels d'achat, toujours sur le benchmark au prix moyen du mois | *a* timing |
| V2 | tes actifs réels au lieu du benchmark, toujours au prix moyen du mois | *b* sélection |
| V3 | tes prix d'exécution réels au lieu du prix moyen | *c* exécution |
| V4 | ton cash réellement resté dormant | *d* cash drag |
| V5 | tes frais réels | *e* frais |
| V6 | tes ventes et arbitrages | *f* sorties |
| Vréel | valeur réelle du portefeuille | — |

- [ ] **Réconciliation exacte, test bloquant (§11)** : `V0 + a + b + c + d + e + f + résidu == Vréel`
      à l'euro près. Le résidu est **toujours** exposé comme une barre « non expliqué », jamais
      absorbé silencieusement dans un terme voisin.
- [ ] Le terme *c* doit être **cohérent avec `execution.py`** : les deux mesurent le même écart
      prix payé / prix moyen du mois. Un test doit vérifier que le signe concorde, sinon la page se
      contredit elle-même d'un bloc à l'autre.
- [ ] **Dépendance au chemin assumée** : l'ordre des substitutions est un choix, le réordonner
      déplace quelques points entre termes adjacents. L'ordre retenu est exposé dans le DTO et
      affiché en note de méthode.
- [ ] **Gate de couverture** : si `window.benchmark_covers_window is False`, le pont est calculé
      sur `clamped_start` et porte un caveat explicite nommant la date de départ réelle ; si la
      partie couverte fait moins de 12 mois, le bloc entier passe `insuffisant` et **aucune valeur
      ne franchit le fil**.
- [ ] Tests : portefeuille qui n'achète que le benchmark aux dates du robot ⇒ tous les termes nuls ;
      un seul achat en fin de période ⇒ terme timing fortement négatif ; réconciliation exacte sur
      un cas construit à la main ; benchmark plus jeune que l'historique ⇒ gate déclenchée.

---

## Task 6 · Assemblage — DTOs, gates, verdicts

**Files:** Modify `dtos/analytics.py`, `services/analytics/report.py` ·
Test `tests/services/analytics/test_report.py`, `tests/routes/test_analytics_routes.py`

- [ ] Étendre `InvestorAnalyticsResponse` avec `execution: ExecutionResponse | None` et
      `counterfactual: CounterfactualResponse | None`. Réutiliser `MetricOut` tel quel — le
      contrat de gate est déjà le bon.
- [ ] `report.py` reste de l'**assemblage pur** : il orchestre `window` → `prices` →
      `counterfactual` / `execution`, il ne calcule rien lui-même. Toute logique qui grossit
      `report.py` est au mauvais endroit.
- [ ] **Le piège de M1, à ne pas refaire** : les verdicts doivent être rédigés à partir des
      **valeurs filtrées par la gate**, jamais des valeurs brutes. En M1, `_verdict` lisait
      `gap`/`gap_eur` bruts et produisait une phrase affirmative sur des chiffres que la gate venait
      de retenir. Un test par bloc doit couvrir « historique court ⇒ verdict de repli ».
- [ ] Étendre les tests de `test_report.py` (sources de données stubées, pattern déjà en place) pour
      couvrir la branche complète des deux nouveaux blocs, pas seulement l'état vide.

---

## Task 7 · Frontend — les deux blocs et leurs graphes

**Files:** Modify `src/types/index.ts`, `src/pages/Analysis.vue` ·
Create `src/components/analytics/AttributionWaterfall.vue`, `SlippageDistribution.vue`

- [ ] Types miroir des nouveaux DTOs, à la suite du bloc `// ── Analytics ──` existant.
- [ ] Les deux graphes suivent le pattern maison : `use([...])` tree-shaken, `VChart`, et
      `useChartResize` — voir `src/components/charts/InvestmentComparisonBarChart.vue` comme
      référence.
- [ ] Le waterfall affiche la barre « non expliqué » quand le résidu est non nul, et l'ordre des
      substitutions en légende.
- [ ] **L'invariant de M1 s'applique aux nouveaux blocs** : une métrique `insuffisant` affiche `—`
      et son caveat, **jamais un nombre**, et un graphe dont la métrique est retenue ne se rend pas
      du tout — il est remplacé par le `ReliabilityBadge` et son explication. Un graphe vide est
      plus honnête qu'un graphe faux.
- [ ] Note de méthode repliable en bas de page : convention TWAP (pas VWAP), dépendance au chemin du
      waterfall, seed fixe des permutations, et la couverture réelle du benchmark.

---

## Task 8 · Le sélecteur de benchmark

**Files:** Create `src/components/analytics/BenchmarkPicker.vue` · Modify `src/pages/Analysis.vue`,
`src/stores/analysis.ts`

**Placement : sur `/analyse`, pas dans les réglages.** L'onglet « Finances » de
`src/pages/Settings.vue:34-39` est **commenté**, donc désactivé volontairement — y poser le
sélecteur le rendrait invisible. Le mettre en tête de `/analyse` le place là où il sert.

- [ ] Liste **curatée d'ETF capitalisants**, avec `IE00B4L5Y983` en défaut. Le caractère
      capitalisant est une **contrainte de correction, pas une préférence** (§7.1) : un ETF
      distribuant a besoin d'une série total-return que l'app ne stocke pas, et donnerait des
      chiffres faux en silence.
- [ ] Saisie libre d'un ISIN autorisée, mais avec avertissement explicite sur les deux risques :
      distribuant, et historique plus court que le portefeuille. La gate de couverture de la Task 2
      est ce qui rend cette liberté sûre.
- [ ] Changer le benchmark écrit `benchmark_asset_key` via `PUT /settings` (le champ existe depuis
      M1) puis invalide le cache du store et relance `fetchAnalytics(force=true)`.
- [ ] **Limite connue à traiter** : `update_settings` (`services/settings.py:231`) garde
      `if data.benchmark_asset_key is not None`, donc le champ ne peut pas être remis à `null`.
      Ajouter le retour au défaut, ou documenter que « défaut » = choisir explicitement
      `IE00B4L5Y983` dans la liste.

---

## Task 9 · Cache serveur (optionnel, à faire en dernier)

La spec (§8) prévoit un mémo serveur clé sur `max(updated_at)` des transactions du user + date du
jour. M1 ne l'a pas construit : le calcul tenait en quelques millisecondes. M2 ajoute 5 000
permutations et un rejeu multi-actifs sur plusieurs années.

- [ ] Mesurer d'abord sur un portefeuille réaliste (~200 achats, 3 ans). **Si le temps de réponse
      reste sous ~1 s, ne pas construire le cache** : le TTL client d'1 h de `stores/analysis.ts`
      suffit, et un cache serveur mal invalidé sert des chiffres périmés — un défaut pire que la
      lenteur qu'il corrige.

---

## Vérification finale de M2

- [ ] `cd capitalview-api && uv run pytest -q` — 0 échec, les 616 tests de M1 inchangés
- [ ] `cd capitalview-web && pnpm type-check && pnpm test && pnpm build` — 0 erreur
- [ ] **Réconciliation du waterfall** : test bloquant vert (somme des termes = valeur réelle)
- [ ] **Calibration des permutations** : p-value uniforme sur données à biais nul
- [ ] **Cohérence inter-blocs** : le terme *exécution* du pont et le slippage agrégé de
      `execution.py` ont le même signe sur le même jeu de données
- [ ] **Vérification navigateur**, comme en fin de M1 : Postgres local + API + `pnpm dev`, login,
      `/analyse`. Contrôler qu'une métrique `insuffisant` rend `—` sans qu'aucun chiffre ne fuite,
      que les graphes correspondants ne se rendent pas, et que changer le benchmark recharge bien
      les deux blocs. Le scénario Playwright de M1 (login → nav → interception de
      `/analytics/investor` avec un payload mixte) est le point de départ à étendre.
- [ ] Fenêtre : vérifier sur un compte à 3 ans d'historique que `ensure_price_history` est appelé
      avec la date du premier achat, et sur un benchmark récent que la gate de couverture se
      déclenche au lieu de produire un chiffre.

## Ce que M2 ne fait pas

La régularité des achats (§2.1), le décalage dépôt→achat (§2.4), le conditionnement au marché
(§2.2), les paris indépendants (§2.3), les frais (§3.1), l'effet de disposition (§3.2) et le bloc
plan cible (§4) restent en M3 — avec `behaviour.py`, `concentration.py`, `fees.py` et `plan.py`.
Le formulaire de saisie du plan cible n'est toujours pas construit : seul le champ de stockage
`investment_plan_enc` existe depuis M1.
