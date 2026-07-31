# Analyse comportementale — Jalon M3 : ce que je fais vraiment, ce que je détiens, ce que ça coûte

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Contexte

M1 a livré la plomberie et l'écart investisseur (§1.1). M2 a livré le pont contrefactuel (§1.2) et
le coût d'exécution (§1.3), plus la matrice de prix, la fenêtre d'analyse et le moteur de
permutation mutualisé. Tout est sur `feat/investor-analytics` dans les deux dépôts.

M3 livre **tout le reste de la spec** : la régularité réelle des achats (§2.1), le décalage
dépôt→achat (§2.4), le conditionnement au marché (§2.2), la diversification réelle (§2.3), les
frais (§3.1), l'effet de disposition (§3.2), le bloc plan cible (§4) — et les deux choses qui
transforment sept encarts en une page : le **verdict global** et la **restructuration en blocs**
prévue au §1 de la spec.

**Ce qui rend ce jalon différent des deux précédents :** M1 et M2 ajoutaient un ou deux blocs à une
page qui en comptait zéro puis un. M3 en ajoute six d'un coup à une page qui en compte déjà trois.
Le risque n'est plus la justesse d'une métrique, c'est que la page devienne le tableau de bord de
trente chiffres que la spec s'interdit explicitement (§12), et que `Analysis.vue` reproduise
`Stock.vue` — 2049 lignes, la raison même pour laquelle une page dédiée a été créée.

**Spec :** `capitalview-api/docs/superpowers/specs/2026-07-29-investor-behaviour-analytics-design.md`
(§2.1, §2.2, §2.3, §2.4, §3.1, §3.2, §4 ; §2 pour le cadre de fiabilité ; §8 pour l'architecture ;
§11 pour les garanties de test)

---

## Décisions actées avec l'utilisateur

| Sujet | Décision |
|---|---|
| Découpage | **Un seul plan, avec un point de coupe explicite** après la Task 7. La partie A (§2.1, §2.4, §2.2 + verdict global + restructuration) est livrable et déployable seule. La partie B (§2.3, §3.1, §3.2, §4) s'ajoute derrière sans rien casser. |
| Tests de composants front | **Non.** Pas de `@vue/test-utils`, pas de jsdom, aucune nouvelle dépendance frontend — cohérent avec M1 et M2. L'invariant de la gate reste garanti côté API par les tests, et côté rendu par la vérification navigateur de fin de jalon. C'est une limite assumée, à réinscrire dans les limites connues. |
| Formulaire de plan cible | **En tête de `/analyse`**, comme le sélecteur de benchmark en M2, et pour la même raison : l'onglet « Finances » de `Settings.vue:34-39` est commenté, donc désactivé — y poser le formulaire le rendrait invisible. |
| Allocation cible | **Par `asset_key` (ISIN) uniquement.** Un libellé libre imposerait une table de correspondance libellé → lignes détenues, sans laquelle la dérive d'allocation n'est pas calculable. |
| Ajouts actés en cours d'exécution | **Taux de rotation** (Task 2), **coût des sorties en euros** et **épisodes clos** (Task 10) — les deux derniers fondus dans un bloc « sorties » unique plutôt qu'en blocs séparés. Les trois portent sur les sorties, que le terme *f* du pont chiffre déjà : la concordance des signes devient un test bloquant. |

**Conséquence de la décision « pas de tests de composants » :** chaque bloc front de ce plan doit
appliquer l'invariant de la gate **dans le template lui-même** (`v-if="metric.value !== null"` sur la
valeur, `v-if` sur le graphe), jamais dans un helper qui pourrait diverger. La vérification finale
liste explicitement chaque bloc à contrôler à l'écran.

---

## Global Constraints

- **Deux dépôts git distincts**, même branche dans les deux. Ne jamais lancer deux agents
  implémenteurs en parallèle sur le même repo ; un agent API + un agent web en parallèle sont sans
  risque.
- **Commits** : conventional commits en anglais, scope quand il est évident, 2-3 lignes maximum,
  pas de liste à puces, **pas de trailer, pas de co-author**.
- **Code, commentaires, docstrings et noms de fichiers en anglais.** Commentaires peu nombreux,
  uniquement là où le *pourquoi* n'est pas évident.
- **Ne rien casser** : `Stock.vue`, les services stock existants et les 8 encarts ne sont pas
  modifiés. La suite existante doit passer inchangée (**677 tests au départ de M3**).
- **Aucune dépendance nouvelle**, ni back ni front. numpy est déjà direct depuis M2 et suffit à
  l'ACP (`np.linalg.eigh`) ; ECharts 6 couvre heatmap, scatter, densité et matrice de corrélation.
- **Argent** : `Decimal` côté stockage et API ; numpy en `float64` en interne, reconversion en
  `Decimal` en sortie.
- **Migrations Alembic** : M3 ne devrait en demander **aucune** — `benchmark_asset_key` et
  `investment_plan_enc` existent depuis M1. Si l'une devenait nécessaire, elle est générée par
  `alembic revision --autogenerate`, **jamais d'ID écrit à la main** (cf. README, ajouté après
  l'incident de M1).
- **Le piège de M1, valable pour les six nouveaux blocs** : un verdict se rédige à partir des
  valeurs **filtrées par la gate**, jamais des valeurs brutes. Un test par bloc doit couvrir
  « échantillon court ⇒ verdict de repli ».

### Piège d'environnement — à traiter avant la première tâche

Le conteneur de développement n'a que **Python 3.14.0rc2**, alors que pydantic 2.12.5 appelle
`typing._eval_type(..., prefer_fwd_module=True)` — paramètre qui n'existe qu'à partir de 3.14 final.
Sans correctif, **toute la suite backend échoue au collect**, ce qui ressemble à s'y méprendre à un
bug du code. Contournement, à réappliquer dans le `.venv` (non versionné) après chaque `uv sync` :
dans `pydantic/_internal/_typing_extra.py::_eval_type`, ne passer `prefer_fwd_module` que si
`inspect.signature(typing._eval_type)` l'accepte.

Les tests ont aussi besoin des variables d'environnement de `.github/workflows/ci.yml`
(`SECRET_KEY`, `DATABASE_URL`, `ENCRYPTION_KEY`, `COMMUNITY_ENCRYPTION_KEY`, `CMC_API_KEY`,
`CG_API_KEY`). Côté front, `node` n'est pas sur le PATH : préfixer avec
`export PATH="$(dirname $(head -1 $(which pnpm) | sed 's|^#!||'))':$PATH"`.

---

## Deux conventions de coût coexistent — et c'est voulu

À écrire dans les notes de méthode, parce que deux conventions différentes dans la même page passent
pour une incohérence si on ne les nomme pas :

| Objet | Convention | Pourquoi |
|---|---|---|
| Effet de disposition (§3.2) | **Coût moyen pondéré** | C'est ce que `get_stock_account_summary` (`services/stock_transaction.py:484-570`) utilise pour le P/L réalisé des 8 encarts. Une autre base ici ferait dire à `/analyse` l'inverse de `/stock` sur les mêmes ventes. C'est aussi la convention d'Odean (1998). |
| Décalage dépôt→achat (§2.4) | **FIFO sur le cash** | On suit un euro déposé jusqu'à son investissement ; le FIFO est la seule lecture qui donne un délai interprétable. Il porte sur des liquidités, pas sur des titres : il ne contredit rien. |

---

## Structure des fichiers

| Fichier | Responsabilité |
|---|---|
| `capitalview-api/services/analytics/behaviour.py` | §2.1 régularité des achats, §2.4 décalage dépôt→achat, §3.2 PGR/PLR. |
| `capitalview-api/services/analytics/timing.py` | *(étendu)* §2.2 conditionnement au marché, au-dessus du moteur de permutation déjà là. |
| `capitalview-api/services/analytics/concentration.py` | §2.3 HHI des poids, N effectif, ACP / entropie de Meucci, corrélations. |
| `capitalview-api/services/analytics/fees.py` | §3.1 total, bps, distribution, **seuil de ticket**, projection 20 ans. |
| `capitalview-api/services/analytics/plan.py` | §4 adhérence en €, dérive d'allocation, écart intention/exécution. No-op si aucun plan. |
| `capitalview-api/services/analytics/report.py` | *(modifié)* assemblage des six blocs, gates, **verdict global**. |
| `capitalview-api/dtos/analytics.py` | *(modifié)* DTOs des six blocs + `verdict` global. |
| `capitalview-api/services/settings.py` | *(modifié)* effacement du plan cible. |
| `capitalview-web/src/components/analytics/VerdictBanner.vue` | Le verdict global, en tête de page. |
| `capitalview-web/src/components/analytics/ContributionHeatmap.vue` | Heatmap calendaire des montants investis. |
| `capitalview-web/src/components/analytics/MarketStateScatter.vue` | Nuage date × drawdown, taille = €. |
| `capitalview-web/src/components/analytics/DensityComparison.vue` | Densité pondérée € vs inconditionnelle. |
| `capitalview-web/src/components/analytics/CorrelationMatrix.vue` | Heatmap de corrélation + N effectif. |
| `capitalview-web/src/components/analytics/InvestmentPlanForm.vue` | Saisie du plan cible. |
| `capitalview-web/src/components/analytics/sections/*.vue` | Un composant par bloc de la spec §1, pour que `Analysis.vue` reste une page d'assemblage. |
| `capitalview-web/src/pages/Analysis.vue` | *(modifié)* réduite à l'assemblage des blocs. |

---

# Partie A — ce que je fais vraiment

## Task 1 · Solder les trois dettes de M2 avant d'empiler

**Pourquoi d'abord.** Les trois sont des corrections courtes, et deux d'entre elles bloquent des
tâches de ce plan. Les traiter en tête évite de construire par-dessus.

**Files:** Modify `capitalview-web/src/pages/Analysis.vue` ·
`capitalview-api/services/analytics/report.py` · `capitalview-api/services/settings.py` ·
`capitalview-web/src/components/analytics/BenchmarkPicker.vue` ·
Test `capitalview-api/tests/routes/test_settings.py`

- [ ] **Les blocs M2 sont invisibles quand `investor_gap` est `null`.** `Analysis.vue:71-75` rend
      l'`BaseEmptyState` et le `v-else` qui suit engloutit le pont et l'exécution. C'est le
      correctif backend n°3 de M2 (« blocs M2 bloqués par les snapshots ») annulé côté front : un
      utilisateur dont le job de snapshots n'a pas tourné ne voit toujours rien. L'état vide ne doit
      s'afficher que si **aucun** bloc n'est disponible, et chaque bloc se rend indépendamment des
      autres. Cette structure est de toute façon celle qu'exige la Task 7.
- [ ] **`trading_days` n'est jamais transmis.** `report.py:153` appelle
      `analyse_execution(transactions, sparse)` sans le paramètre, donc `get_non_trading_days`
      (`services/market.py:67`) n'est jamais consulté et le repli « jours cotés » est le seul chemin
      réellement emprunté. Trancher explicitement : soit câbler le calendrier depuis
      `MarketAsset.exchange`, soit supprimer le paramètre mort. **Recommandé : câbler**, la
      Task 4 a besoin de la même notion de jour de bourse pour permuter sur toute la fenêtre.
- [ ] **`investment_plan` ne peut pas être remis à `null`.** `services/settings.py:236` garde
      `if data.investment_plan is not None`. M2 a corrigé le benchmark (`.strip() or None`), pas le
      plan. Sans ça, un plan saisi une fois est indélébile — bloquant pour la Task 10. Traiter un
      dict vide comme une suppression (`settings.investment_plan_enc = None`), avec un test qui
      couvre le tour complet : saisie → lecture → effacement → lecture.
- [ ] **Saisie libre d'ISIN dans le sélecteur de benchmark** : prévue au plan M2, non implémentée
      (`BenchmarkPicker.vue` n'expose qu'une liste). Soit l'ajouter avec son avertissement explicite
      (distribuant / historique trop court), soit acter la liste curatée comme définitive dans les
      notes de méthode. La gate de couverture de M2 rend les deux options sûres.
- [ ] `uv run pytest -q` et `pnpm type-check && pnpm test && pnpm build` — verts.

---

## Task 2 · §2.1 — la régularité réelle, mesurée sur les achats

**Files:** Create `services/analytics/behaviour.py` ·
Test `tests/services/analytics/test_behaviour.py`

**Produces:** `PurchaseRegularity` (dataclass frozen) et
`analyse_purchase_regularity(transactions, window) -> PurchaseRegularity`

**Calcul**, sur les montants **achetés par mois** (`amount × price_per_unit` des `BUY`, EUR exclu) —
jamais sur les dépôts (§0 bis) :

- `months_with_purchase / total_months` ;
- coefficient de variation des montants mensuels investis (écart-type / moyenne, mois à zéro
  inclus — les exclure transformerait un investisseur intermittent en investisseur régulier) ;
- plus longue interruption, en mois consécutifs sans achat ;
- **HHI temporel** : `Σ (mᵢ / M)²` sur les montants mensuels, et son inverse `1/HHI` =
  **nombre d'achats mensuels égaux équivalents** ;
- **régularité du jour du mois** : dispersion (écart-type circulaire ou IQR sur le jour) de l'achat
  principal de chaque mois.

- [ ] Le total de mois est celui de la fenêtre (`window.start` → `window.end`), pas le nombre de mois
      où il s'est passé quelque chose : un trou de six mois **est** l'information.
- [ ] L'HHI temporel est un usage maison de l'indice de Herfindahl–Hirschman sur l'axe du temps
      (§2.1 de la spec) — le dire dans la docstring, et la note de méthode le reprendra.
- [ ] Gates : `insuffisant` sous 6 mois de fenêtre ou moins de 3 achats ; `indicatif` sous 24 mois ;
      `solide` au-delà. La dispersion du jour du mois porte sa propre gate : `insuffisant` sous
      10 achats.
- [ ] Sortie : la série mensuelle complète (`[(année, mois, montant)]`) accompagne les cinq
      chiffres — c'est la source de la heatmap, et elle suit la gate comme le reste.
- [ ] **Taux de rotation** (ajout acté après rédaction du plan) : `min(achats, ventes) / capital
      moyen`, **annualisé** — sans annualisation le chiffre n'est comparable à aucune littérature.
      C'est la variable canonique de Barber & Odean (2000), déjà en référence de la spec, et elle
      coûte presque rien puisque les achats sont déjà agrégés. **Le verdict « tu trades trop » ne
      sort que si le chiffre est élevé** : sur un accumulateur d'ETF il sortira proche de zéro, et
      la phrase correcte est alors « tu ne tournes pas ton portefeuille, ce n'est pas là que ça se
      joue ».
- [ ] Tests : 24 mois à montant identique ⇒ CV 0, HHI = 1/24, équivalent 24 ; tout le capital sur un
      mois ⇒ HHI 1, équivalent 1 ; trou de 5 mois détecté ; achats systématiquement le 5 ⇒
      dispersion faible ; fenêtre de 3 mois ⇒ `insuffisant` et **aucune valeur sérialisée**.

---

## Task 3 · §2.4 — le décalage dépôt → investissement

**Files:** Modify `services/analytics/behaviour.py` ·
Test `tests/services/analytics/test_behaviour.py`

**Produces:** `DepositLag` et `analyse_deposit_lag(transactions) -> DepositLag`

**Algorithme.** File FIFO sur les liquidités : chaque dépôt externe **réel** (provisions
automatiques exclues, `flows.is_auto_provision`) entre dans la file avec sa date ; chaque `BUY`
consomme la file dans l'ordre. Le délai d'un euro est `date_achat − date_dépôt`, et la distribution
est pondérée par les euros.

- [ ] **Les achats financés par une provision automatique n'ont pas de dépôt réel à apparier.** Ils
      consommeraient la file de travers et fabriqueraient un délai qui n'existe pas. Règle : la part
      d'achat non couverte par la file réelle est **comptée à part** (`unmatched_share`), jamais
      appariée de force.
- [ ] Gate spécifique : si `unmatched_share > 0.50`, le bloc entier passe `insuffisant` avec un
      caveat qui nomme la cause — « la date réelle d'entrée de ton argent est inconnue sur plus de la
      moitié de tes achats » (§7.2 de la spec). C'est le risque n°1 du tableau §12.
- [ ] Sorties : médiane, Q1, Q3, queue (P90) du délai ; **les deux rythmes côte à côte** — la
      régularité des dépôts calculée avec exactement les mêmes indicateurs que la Task 2, pour que la
      comparaison soit une comparaison et pas deux mesures différentes ; et le rappel du coût du
      délai, déjà chiffré en M2 (`bridge.idle_cash_opportunity`), rattaché ici à sa cause.
- [ ] Tests : dépôt puis achat le lendemain ⇒ délai 1 j ; dépôt unique consommé par trois achats
      étalés ⇒ délais pondérés corrects ; achats sans dépôt réel ⇒ `unmatched_share = 1` et bloc
      `insuffisant` ; dépôt jamais investi ⇒ n'entre pas dans la distribution des délais mais reste
      dans le cash dormant.

---

## Task 4 · §2.2 — contrarian ou suiveur ?

**Files:** Modify `services/analytics/timing.py` ·
Test `tests/services/analytics/test_timing.py`

**Produces:** `MarketConditioning` et
`analyse_market_conditioning(buys, benchmark_series, trading_days, *, draws, rng) -> MarketConditioning`

Le §8 de la spec place ce bloc dans `timing.py`, avec le moteur de permutation. On l'y met, sous le
moteur, sans le mêler à lui : le moteur reste ignorant de ce qu'il teste.

**Calcul.** Pour chaque jour de bourse : (a) drawdown depuis le plus-haut **glissant sur 1 an** du
benchmark, (b) rendement des **21 derniers jours de bourse**. On compare la distribution **pondérée
par les euros achetés** à la distribution **inconditionnelle** de tous les jours de bourse de la
fenêtre.

- [ ] **Le piège du plus-haut glissant.** Au premier jour de la fenêtre, un maximum sur 1 an n'existe
      pas : `resolve_window` ne backfille qu'à partir de `window.start`. Sans traitement, les premiers
      mois affichent un drawdown quasi nul et l'utilisateur se croit contrarian. Deux options, à
      trancher dans cette tâche : **étendre le backfill du benchmark de 365 jours en amont**
      (recommandé — `ensure_price_history(session, benchmark_key, ..., start - 365 j)` dans
      `resolve_window`), ou marquer les jours dont la fenêtre glissante est incomplète et les exclure
      des deux distributions. Ne pas laisser le cas implicite.
- [ ] Le rendement 21 jours se compte en **jours de bourse**, pas en jours calendaires : la série
      forward-fillée répéterait la même clôture et écraserait le signal. Utiliser les jours
      réellement cotés.
- [ ] **Permutation** : les dates d'achat sont permutées sur l'ensemble des jours de bourse de la
      fenêtre, montants constants, `DEFAULT_DRAWS` tirages, seed fixe. Hypothèse nulle : « mon argent
      entre un jour au hasard ». Statistique observée : drawdown moyen pondéré par les euros.
      Réutiliser `permutation_test` **sans le modifier**.
- [ ] `p > 0.10` ⇒ le verdict devient « rien de détectable ». **Jamais** « tu es bon ».
- [ ] **Découpage année 1 / année 2**, chacun étiqueté « tendance, pas preuve » (12 mois par bucket,
      §2 de la spec). Le découpage n'apparaît que si la fenêtre couvre au moins 24 mois.
- [ ] Gate : `insuffisant` sous 10 achats ou sous 250 jours de bourse dans la fenêtre.
- [ ] Tests : achats concentrés sur les plus bas ⇒ drawdown moyen pondéré nettement inférieur à
      l'inconditionnel et `p` faible ; achats à dates aléatoires ⇒ `p` élevé et verdict « rien de
      détectable » ; fenêtre de 18 mois ⇒ pas de découpage annuel ; fenêtre partielle en tête ⇒ le
      traitement du plus-haut glissant retenu est appliqué et testé.

---

## Task 5 · Le verdict global

**Files:** Modify `services/analytics/report.py`, `dtos/analytics.py` ·
Test `tests/services/analytics/test_report.py`

Le §1 de la spec ouvre la page par « 3 à 5 phrases générées, brutales, avec les chiffres dedans ».
C'est le livrable qui fait la différence entre une page de chiffres et une page qui sert.

- [ ] `build_global_verdict(blocks) -> str` : sélectionne les **trois à cinq constats les plus
      coûteux**, ordonnés par euros en jeu, et les formule avec leurs chiffres.
- [ ] **Il ne lit que des blocs ayant franchi leur gate.** Un bloc `insuffisant` n'entre pas dans le
      verdict, et son absence n'est pas silencieuse : quand moins de trois constats sont disponibles,
      le verdict dit ce qui manque et pourquoi. C'est exactement le défaut corrigé en clôture de M1,
      généralisé à sept blocs.
- [ ] Quand un bloc dit « rien de détectable » (permutation non concluante), le verdict le reprend
      comme un résultat — « ce n'est pas là qu'il faut chercher » — et non comme une absence.
- [ ] Tests : portefeuille vide ⇒ verdict de repli, aucun chiffre ; portefeuille riche ⇒ les constats
      apparaissent dans l'ordre du coût ; un bloc gaté ⇒ **aucune de ses valeurs n'apparaît dans la
      chaîne** (test par recherche de sous-chaîne sur les nombres retenus).

---

## Task 6 · Assemblage partiel — DTOs et gates de la partie A

**Files:** Modify `dtos/analytics.py`, `services/analytics/report.py` ·
Test `tests/services/analytics/test_report.py`, `tests/routes/test_analytics_routes.py`

- [ ] Étendre `InvestorAnalyticsResponse` : `verdict: str`, `regularity: RegularityResponse | None`,
      `deposit_lag: DepositLagResponse | None`, `market_conditioning: MarketConditioningResponse | None`.
      Réutiliser `MetricOut` partout — le contrat de gate est déjà le bon.
- [ ] `report.py` reste de l'**assemblage pur**. Les trois nouveaux blocs consomment la fenêtre et la
      matrice déjà résolues par `_replay_blocks` : **aucun appel réseau supplémentaire**, aucune
      seconde résolution de fenêtre.
- [ ] **Champs absents du DTO silencieusement filtrés par FastAPI** : le défaut n°5 de M2. Un test
      verrouille la forme complète de chaque nouvelle réponse.
- [ ] Les trois blocs ne dépendent que des transactions et des prix : ils ne passent **pas** derrière
      la sortie anticipée `len(series) < 2` (défaut n°3 de M2).

---

## Task 7 · Front — restructuration en blocs et Bloc 1

**Files:** Modify `src/types/index.ts`, `src/pages/Analysis.vue` ·
Create `src/components/analytics/VerdictBanner.vue`, `ContributionHeatmap.vue`,
`MarketStateScatter.vue`, `DensityComparison.vue`, `src/components/analytics/sections/`

**La restructuration d'abord, le contenu ensuite.** La page adopte le découpage du §1 de la spec :

```
/analyse
├── Sélecteur de benchmark + (M3) formulaire de plan
├── Verdict
├── Bloc 1 · Ce que je fais vraiment      (§2.1, §2.4, §2.2)
├── Bloc 2 · Ce que ça me coûte           (§1.1, §1.2, §1.3 — déjà là, déplacés)
├── Bloc 3 · Ce que je détiens vraiment   (§2.3)         ← partie B
├── Bloc 4 · Frais                        (§3.1)         ← partie B
├── Bloc 5 · Adhérence au plan            (§4)           ← partie B
└── Notes de méthode + ce que je ne calcule pas
```

- [ ] Un composant de section par bloc, `Analysis.vue` réduite à l'assemblage. Sans ça, la page
      dépasse le millier de lignes à la Task 12 et devient le `Stock.vue` que la spec cite comme
      contre-exemple.
- [ ] Types miroir des nouveaux DTOs, à la suite du bloc `// ── Analytics ──`.
- [ ] Graphes selon le pattern maison : `use([...])` tree-shaken, `VChart`, `useChartResize` — voir
      `src/components/charts/InvestmentComparisonBarChart.vue`.
- [ ] **Invariant de la gate, dans le template** (décision « pas de tests de composants ») : une
      métrique `insuffisant` rend `—` et son caveat, **jamais un nombre** ; un graphe dont la métrique
      est retenue **ne se rend pas du tout**, remplacé par le `ReliabilityBadge` et son explication.
      Un graphe vide est plus honnête qu'un graphe faux.
- [ ] Le verdict global en tête, visuellement au-dessus des blocs, pas une carte parmi d'autres.
- [ ] Notes de méthode enrichies : HHI temporel comme usage maison, FIFO du cash vs coût moyen
      pondéré (tableau ci-dessus), traitement du plus-haut glissant retenu en Task 4, « tendance, pas
      preuve » pour le découpage annuel.

### ✂️ Point de coupe

**À cet endroit le jalon est livrable :** trois nouveaux blocs en ligne, verdict global, page
restructurée, dettes M2 soldées. `pytest -q`, `pnpm type-check && pnpm test && pnpm build` verts et
vérification navigateur faite, on peut s'arrêter ici et reprendre la partie B plus tard sans laisser
la page dans un état intermédiaire.

---

# Partie B — ce que je détiens, ce que ça coûte, ce que j'avais prévu

## Task 8 · §2.3 — le nombre de paris indépendants

**Files:** Create `services/analytics/concentration.py` ·
Test `tests/services/analytics/test_concentration.py`

**Produces:** `Concentration` et
`analyse_concentration(holdings, price_matrix) -> Concentration`

Trois chiffres qui divergent, et c'est tout l'intérêt : lignes détenues, **nombre effectif de
positions** `1/HHI` sur les poids, **nombre effectif de paris indépendants** (ACP sur la covariance
des rendements journaliers, exposition du portefeuille aux composantes principales, entropie des
contributions à la variance : `N_ent = exp(−Σ pᵢ ln pᵢ)`).

- [ ] **La matrice sparse, jamais la remplie.** Les prix forward-fillés répètent la clôture
      précédente : sur les week-ends et jours fériés, toutes les lignes affichent un rendement nul
      simultané, ce qui **gonfle mécaniquement les corrélations**. Les rendements se calculent sur les
      jours réellement cotés, en **intersection par paire**.
- [ ] Gate de la spec (§2) : **minimum 250 rendements journaliers chevauchants par paire**, et au
      moins 2 lignes. Une paire sous le seuil n'entre pas dans la matrice de covariance ; si trop de
      paires tombent, le bloc entier passe `insuffisant`.
- [ ] **Plafonné à `indicatif`, jamais `solide`** : la spec le dit explicitement (« 2 ans de
      rendements journaliers ⇒ estimation bruitée, l'ACP sur peu d'actifs est sensible »). La taille
      d'échantillon est affichée.
- [ ] Poids = quantités détenues à `window.end` × prix terminal (`price_end`, déjà calculé par
      `report.py:148`). Le cash EUR est exclu des poids : il n'est pas un pari.
- [ ] **Ce que ce n'est pas, et le code le dit** : pas de look-through. On mesure la redondance de
      **comportement**, pas la composition des ETF. Le mot « look-through » n'apparaît que pour dire
      qu'on ne le fait pas.
- [ ] Tests : deux actifs parfaitement corrélés ⇒ ~1 pari indépendant ; deux actifs décorrélés à
      poids égaux ⇒ ~2 ; une ligne à 98 % et une à 2 % ⇒ N effectif proche de 1 ; historique de
      100 jours ⇒ `insuffisant` et aucune valeur sérialisée ; ACP sur une covariance dégénérée
      (actif constant) ⇒ pas d'exception, bloc gaté.

---

## Task 9 · §3.1 — le seuil de rentabilité du ticket moyen

**Files:** Create `services/analytics/fees.py` · Test `tests/services/analytics/test_fees.py`

**Produces:** `FeeAnalysis` et `analyse_fees(transactions, window) -> FeeAnalysis`

- [ ] Frais totaux (champ `fees` de chaque transaction), en % du capital déployé, en **bps
      annualisés**, distribution des frais par ordre rapportée à la taille d'ordre.
- [ ] **Le chiffre qui change un comportement, c'est le seuil**, pas le total : frais moyen par
      ordre ÷ 25 bps = taille d'ordre en dessous de laquelle on dépasse 25 bps de frais d'entrée.
      Compter les ordres sous le seuil et chiffrer ce qu'ils ont coûté.
- [ ] **Projection 20 ans avec l'hypothèse écrite dans la sortie**, pas seulement dans le code :
      même cadence de versement, 5 %/an. Une projection dont l'hypothèse n'est pas affichée est une
      affirmation déguisée.
- [ ] **Note d'honnêteté obligatoire, portée par le DTO** (§3.1) : les frais de courtage ne sont pas
      le coût principal ; le **TER des ETF** (0,15–0,25 %/an) est déjà dans le cours et **n'est pas
      traçable ici**. Sur un portefeuille buy-and-hold, il pèse structurellement plus lourd. Ce texte
      n'est pas conditionnel : il s'affiche même quand les frais d'ordre sont nuls.
- [ ] Gate : `insuffisant` sous 5 ordres ; `indicatif` sous 20.
- [ ] Tests : frais nuls ⇒ seuil non calculable, note TER quand même présente ; seuil correct sur un
      cas construit ; comptage des ordres sous le seuil ; projection cohérente avec l'hypothèse
      déclarée.

---

## Task 10 · §3.2 — ce que tu fais de tes sorties

*Périmètre élargi après rédaction du plan : PGR/PLR, le coût en euros et les épisodes clos vivent
dans **un seul bloc** sous **une seule gate**. Ils mesurent la même chose — comment les sorties sont
gérées — sur la même donnée. Trois blocs afficheraient trois fois « données insuffisantes » sur un
portefeuille d'accumulateur.*

**Files:** Modify `services/analytics/behaviour.py` ·
Test `tests/services/analytics/test_behaviour.py`

**Produces:** `Exits` et `analyse_exits(transactions, price_matrix, benchmark_series) -> Exits`

### 10a · Effet de disposition (PGR/PLR) — la mesure canonique

Odean (1998) : à chaque jour de vente, compter les gains latents **réalisés** vs **disponibles**
(PGR) et les pertes latentes réalisées vs disponibles (PLR). `PGR/PLR > 1` ⇒ tu coupes tes gains et
gardes tes pertes.

- [ ] **Base de coût = coût moyen pondéré**, alignée sur `get_stock_account_summary` (voir le tableau
      des conventions). Toute autre base ferait diverger `/analyse` et `/stock` sur les mêmes ventes.
- [ ] Le prix du jour de vente vient de la matrice ; une ligne sans cotation ce jour-là est comptée
      dans les « non évaluables », pas silencieusement ignorée.
- [ ] **On le garde**, malgré le chiffrage en euros de 10b : sans lui, 450 € ne se diagnostiquent pas
      — habitude de couper les gains, ou une seule sortie ratée ? Le ratio dit lequel.

### 10b · Le coût des sorties, en euros

Odean mesure exactement ça : le rendement des titres vendus contre celui des titres conservés, à
84 / 252 / 504 jours. Un ratio ne s'actionne pas, un montant si.

- [ ] **Horizon fixe et affiché : 1 an, tronqué à aujourd'hui.** « Le rendement futur » n'existe pas
      sans horizon. Les ventes trop récentes pour avoir l'horizon sont **exclues et comptées**, jamais
      évaluées sur trois semaines.
- [ ] **Base de comparaison : le benchmark**, pas « le reste du portefeuille ». Le reste change de
      composition pendant l'horizon — c'est une cible mouvante, et sur un portefeuille à deux lignes
      « le reste » est une ligne. La série du benchmark est déjà chargée. La variante « vs reste du
      portefeuille » peut être exposée en second, **jamais en chiffre-titre**.
- [ ] Coût = `(rendement du titre vendu − rendement du benchmark) × montant vendu`, sommé.
- [ ] **Plafonné à `indicatif`, jamais `solide`** : trois ventes qui produisent un montant à trois
      chiffres, c'est précisément la fausse confiance que le §2 existe pour empêcher.

### 10c · Épisodes clos : hit rate et payoff ratio

Un épisode va du premier achat d'une ligne à sa revente **totale**. Combien sont gagnants (hit rate),
et les gagnants couvrent-ils les perdants (payoff ratio) ?

- [ ] Gate propre : **20 épisodes clos minimum**. En dessous, « tu as raison 60 % du temps » veut dire
      trois gagnantes sur cinq. Un accumulateur d'ETF en aura zéro, et c'est un résultat.
- [ ] Un rachat de la même ligne après une revente totale ouvre un **nouvel** épisode.

### Gate et cohérence du bloc

- [ ] **Gate unique à 12 occasions de réalisation** pour 10a et 10b ; 10c porte sa propre gate à 20
      épisodes. Sous le seuil, aucun chiffre — et le message d'insuffisance est lui-même le verdict :
      « tu as vendu 3 fois en 2 ans, c'est trop peu pour mesurer quoi que ce soit. **C'est en soi
      l'information : tu es un accumulateur, pas un arbitragiste.** L'effet de disposition n'est pas
      ton problème — les métriques d'apport le sont. »
- [ ] **Test bloquant de cohérence inter-blocs** : le coût des sorties de 10b et le **terme *f***
      (« effet des sorties ») du pont contrefactuel mesurent le même phénomène. Leurs signes doivent
      concorder sur le même jeu de données, sinon la page se contredit d'un bloc à l'autre — même
      contrôle que celui déjà exigé entre `execution.py` et le terme *c*.
- [ ] Tests : ventes systématiques en gain ⇒ PGR/PLR > 1 ; 3 ventes ⇒ `insuffisant`, aucune valeur et
      verdict d'accumulateur ; portefeuille sans vente ⇒ bloc présent avec son message, pas absent ;
      vente d'il y a deux mois ⇒ exclue de 10b et comptée ; ligne revendue puis rachetée ⇒ deux
      épisodes.

---

## Task 11 · §4 — le plan cible et son formulaire

**Files:** Create `services/analytics/plan.py` · Modify `dtos/analytics.py` ·
Test `tests/services/analytics/test_plan.py`

**Forme du plan** (JSON chiffré dans `investment_plan_enc`, existant depuis M1) :

```json
{"monthly_target": "500", "allocation": {"IE00B4L5Y983": "80", "IE00BKM4GZ66": "20"}, "since": "2026-01"}
```

- [ ] **`since` est le champ que la spec ne prévoyait pas et dont ce bloc a besoin.** Sans lui, un
      plan déclaré aujourd'hui est appliqué rétroactivement sur trois ans et produit un verdict
      d'adhérence faux et démoralisant. Défaut : le mois du premier achat, affiché comme tel dans
      l'UI, modifiable.
- [ ] **Adhérence en €** : écart cumulé entre le plan et ce qui a été **investi** (pas déposé), mois
      par mois, en € et en % du plan. **Mois complets uniquement** — le mois courant, encore en
      cours, ferait apparaître un sous-investissement systématique.
- [ ] **Dérive d'allocation** : norme L1 entre allocation réelle et cible, en points, plus le montant
      à rééquilibrer (somme des écarts positifs × valeur du portefeuille). Une ligne détenue absente
      de la cible compte comme cible 0 %, et une cible non détenue compte comme réel 0 %.
- [ ] **Écart intention / exécution** : la comparaison la plus dure. Inclure le croisement avec l'état
      du marché — la part des mois sous-investis qui sont des mois de baisse du benchmark — en
      réutilisant la série de la Task 4, sans recalcul.
- [ ] Bloc **entièrement absent** (`None`) si aucun plan n'est déclaré. Toutes les autres métriques de
      la page restent calculées et affichées sans plan (§4 de la spec).
- [ ] Validation : allocation dont la somme s'écarte de 100 % ⇒ rejet à la saisie avec un message
      clair, jamais une normalisation silencieuse.
- [ ] Tests : plan absent ⇒ `None` et aucune autre métrique affectée ; plan respecté à l'euro ⇒
      adhérence 100 %, dérive 0 ; sous-investissement de 30 % ⇒ chiffre exact ; `since` postérieur au
      premier achat ⇒ les mois antérieurs sont exclus ; allocation à 90 % ⇒ rejet.

---

## Task 12 · Assemblage final et front de la partie B

**Files:** Modify `services/analytics/report.py`, `dtos/analytics.py`, `src/pages/Analysis.vue`,
`src/types/index.ts` · Create `CorrelationMatrix.vue`, `InvestmentPlanForm.vue`, sections des blocs
3 à 5

- [ ] Les trois blocs restants dans `InvestorAnalyticsResponse` ; **verdict global enrichi** des
      nouveaux constats, toujours à partir des seules valeurs gatées.
- [ ] `InvestmentPlanForm.vue` en tête de `/analyse`, à côté du sélecteur de benchmark : montant
      mensuel, allocation par `asset_key` avec les lignes détenues en suggestions, mois de départ, et
      **un bouton d'effacement** (rendu possible par la Task 1). Écriture via `PUT /settings`, puis
      invalidation du cache et `fetchAnalytics(force=true)`.
- [ ] Ajouter `investment_plan` aux types `UserSettingsUpdate` / `UserSettingsResponse` de
      `src/types/index.ts` — le champ existe côté API depuis M1 mais n'est **pas** déclaré côté web.
- [ ] Bloc 5 masqué tant qu'aucun plan n'est déclaré, remplacé par une invitation courte à en
      déclarer un — pas par un vide.
- [ ] **Section repliable « ce que je ne calcule pas, et pourquoi »** (§5 de la spec) : Sharpe /
      Sortino / ratio d'information, alpha / bêta, max drawdown en chiffre-titre, Brinson–Fachler
      sectoriel, implementation shortfall — chacun avec sa raison. Un indicateur absent doit être un
      choix visible, pas un oubli.
- [ ] Notes de méthode complétées : plafond `indicatif` de l'ACP, absence de look-through, hypothèse
      de la projection de frais, note TER, base de coût moyen pondéré du PGR/PLR, `since` du plan.

---

## Task 13 · Performance — re-mesurer, puis trancher le cache

M2 a mesuré **0,235 s** sur le portefeuille de contrôle (35 achats, 760 jours, 2 actifs) et a
**refusé le cache serveur** : sous ~1 s, un cache mal invalidé sert des chiffres périmés, défaut pire
que la lenteur qu'il corrige. M3 ajoute 5 000 permutations supplémentaires (§2.2), une ACP et une
matrice de corrélation.

- [ ] Re-mesurer sur le même portefeuille de contrôle **et** sur un profil plus lourd (~200 achats,
      3 ans, 6 lignes) — l'ACP croît avec le nombre de lignes, pas avec le nombre d'achats.
- [ ] **Si le calcul reste sous ~1 s, ne pas construire le cache serveur.** Le TTL client d'1 h
      suffit. Reprendre la décision de M2 explicitement, chiffres à l'appui.
- [ ] Rappeler dans les limites connues que le vrai poste de latence reste `ensure_price_history`,
      qui retente le réseau à chaque requête (~26 s hors réseau pour 0,235 s de calcul) — préexistant
      à la couche marché, hors périmètre, mais premier levier si la latence devient un sujet.

---

## Vérification finale de M3

- [ ] `cd capitalview-api && uv run pytest -q` — 0 échec, les 677 tests de M2 inchangés
- [ ] `cd capitalview-web && pnpm type-check && pnpm test && pnpm build` — 0 erreur
- [ ] **Calibration des permutations** (§11) : sur données synthétiques à biais nul, la p-value du
      bloc §2.2 est approximativement uniforme sur [0,1], comme celle du bloc §1.3 en M2
- [ ] **Gates** : pour chacun des six nouveaux blocs, un test vérifie que sous le seuil la métrique
      bascule en `insuffisant` **et que la valeur n'est pas sérialisée** dans la réponse
- [ ] **Cohérence inter-blocs** : le coût du délai affiché en §2.4 est celui du terme *d* du pont
      (M2) — même nombre, deux endroits, jamais deux calculs
- [ ] **Non-régression** : les 8 encarts de `/stock` et les courbes existantes sont inchangés
- [ ] **Vérification navigateur** (c'est le seul filet côté rendu, décision actée) : Postgres local +
      API + `pnpm dev`, login, `/analyse`. Contrôler bloc par bloc — régularité, décalage,
      conditionnement, concentration, frais, disposition, plan — qu'une métrique `insuffisant` rend
      `—` sans qu'aucun chiffre ne fuie et que **son graphe ne se rend pas**. Vérifier le verdict
      global sur un portefeuille pauvre (repli, aucun chiffre) et sur un portefeuille riche.
- [ ] **Formulaire de plan** : saisie → le Bloc 5 apparaît ; effacement → il disparaît et les autres
      blocs sont intacts ; allocation à 90 % → rejet avec message.

## Ce que M3 ne fera pas

- **Look-through des ETF** (§6 #3) : exige une source externe ou un taggage manuel. La diversification
  reste une mesure de **redondance de comportement**, et l'UI le dit.
- **Journal de décision structuré** (§6 #4) et **horodatage de décision** (§6 #5) : signalés par la
  spec, non construits. Sans horodatage, pas d'implementation shortfall — et on ne prétend pas
  l'avoir.
- **Crypto** : hors périmètre depuis le §0 de la spec. Le moteur reste agnostique.
- **Tests de composants front** : décision actée, limite assumée et réinscrite dans les limites
  connues du jalon.
- Tout ce que le §5 écarte : Sharpe, Sortino, ratio d'information, alpha/bêta, max drawdown en
  chiffre-titre, Brinson–Fachler sectoriel.

---

## Résultats de l'exécution

Toutes les tâches sont exécutées et poussées sur `claude/investor-analytics-m3-plan-fhs6l7` dans les
deux dépôts. Le point de coupe après la Task 7 a été franchi sans incident : la partie A a été
poussée seule et vérifiée avant que la partie B ne démarre.

- Backend **761 passed / 0 failed** (677 au départ de M3, 714 au point de coupe)
- Frontend **25 passed**, `type-check` et `build` clean
- **Cohérence inter-blocs** : trois tests bloquants dans `test_cross_block_coherence.py` — une sortie
  qui détruit de la valeur est négative dans le pont *et* positive en coût dans le bloc sorties, et
  réciproquement ; le slippage en bps et le terme *c* du pont gardent des signes opposés
- **Calibration des permutations** : conservée depuis M2, et le nouveau bloc §2.2 sort `p = 0,73`
  sur des achats non conditionnés — « rien de détectable », comme attendu
- **Vérification navigateur complète** sur PostgreSQL local avec un portefeuille de 96 transactions :
  9 titres de blocs rendus, 10 graphiques, 24 valeurs `—` pour les métriques gatées, zéro erreur de
  page. Le cycle complet du plan cible a été rejoué à l'écran : allocation à 70 % refusée, plan
  enregistré → bloc affiché, plan supprimé → bloc disparu, les autres blocs intacts.

### Task 13 tranchée : toujours pas de cache serveur

Mesure du calcul pur, prix déjà en base (`scratchpad/bench.py`, market et base stubés) :

| Scénario | Temps |
|---|---|
| Contrôle M2 — 35 ordres, 2 actifs, 2 ans | **0,053 s** |
| Réaliste — 200 ordres, 6 actifs, 3 ans | **0,159 s** |
| Stress — 500 ordres, 10 actifs, 5 ans | **0,352 s** |

Très en dessous du seuil de ~1 s, même en ajoutant l'ACP et les 5 000 permutations du §2.2. Le TTL
client d'1 h suffit, et la décision de M2 tient : un cache serveur mal invalidé servirait des
chiffres périmés, défaut pire que la lenteur qu'il corrigerait.

**En revanche la latence réelle observée est de 40 s**, intégralement passée dans
`ensure_price_history` qui retente le réseau à chaque requête même quand la base est à jour. C'est
le défaut préexistant signalé en fin de M2 ; il est maintenant chiffré en conditions réelles, et
c'est le seul levier de performance qui compte.

## Défauts trouvés pendant l'exécution et corrigés

1. **Les blocs M2 étaient invisibles côté front** quand `investor_gap` était `null` — le correctif
   backend n°3 de M2 s'était arrêté à mi-chemin. Chaque bloc se rend désormais sur ses propres
   données (Task 1).
2. **Le calendrier d'échange n'était jamais consulté** : `report.py` n'a jamais passé `trading_days`
   à `analyse_execution`. Câblé via `resolve_trading_days` (Task 1).
3. **Le plan cible ne pouvait pas être supprimé** — un dict vide efface désormais le champ (Task 1).
4. **Le plus-haut glissant du §2.2 n'existait pas au début de la fenêtre.** `resolve_window`
   backfille maintenant le benchmark un an plus tôt, et les séances sans année glissante complète
   sont écartées. Sans ça, les premiers mois affichaient un drawdown quasi nul et se lisaient comme
   des achats dans le creux.
5. **Double backfill du benchmark quand il est aussi une ligne détenue** : une seule requête, à la
   date la plus ancienne des deux besoins.
6. **`report.py` lisait `investment_plan` sur le modèle ORM**, qui ne porte que
   `investment_plan_enc`. Le bloc plan restait donc muet en production alors que les tests
   passaient — le stub de test exposait un champ en clair qui n'existe nulle part. Trouvé par la
   vérification navigateur, pas par la suite. Le test utilise désormais un vrai chiffrement.
7. **Le formulaire de plan plantait au clic sur « Enregistrer »** : `BaseInput` en `type="number"`
   émet un nombre, et le formulaire appelait `.trim()` dessus. Le bouton ne faisait rien, sans
   message d'erreur.
8. **Le verdict de concentration affichait « 1.3627 pari indépendant »** — quatre décimales dans une
   phrase. Arrondi dans la prose, précision conservée dans la métrique.
9. **Les parts et taux s'affichaient signés** (« +53,85 % de mois investis ») : `formatPercent`
   signe toujours, et une part n'est pas une variation.

## Limites connues, assumées

- **Toujours pas de test de composant** sur les `.vue` : décision actée en tête de plan. L'invariant
  de la gate est garanti côté API par les tests et côté rendu par la vérification navigateur. Le
  défaut n°7 ci-dessus est exactement ce qu'un test de composant aurait attrapé sans navigateur —
  c'est le coût de la décision, et il est réel.
- **`ensure_price_history` retente le réseau à chaque requête**, d'où 40 s de latence pour 0,2 s de
  calcul. Hors périmètre M3, mais désormais chiffré.
- **Le hit rate et le payoff ratio n'ont rien à mesurer** sur un portefeuille d'accumulateur : zéro
  épisode clos. Le bloc le dit plutôt que de rester vide, et sa gate à 20 épisodes tiendra le jour
  où des positions seront réellement soldées.
- **La vérification navigateur a tourné sur des prix synthétiques** : le conteneur n'a pas d'accès
  aux marchés, les séries ont donc été générées en base. Les formes, les gates et les verdicts sont
  vérifiés ; les montants affichés ne le sont pas.

## Ce que M3 ne fait pas

Le look-through des ETF, le journal de décision structuré et l'horodatage de décision (§6 de la
spec) restent signalés et non construits. La crypto reste hors périmètre. Tout ce que le §5 écarte
est désormais listé à l'écran dans la section repliable « ce que cette page ne calcule pas ».
