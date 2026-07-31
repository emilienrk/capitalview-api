# Analyse comportementale — Jalon M4 : corriger, alléger, configurer

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Context

M1, M2 et M3 ont livré la page `/analyse` complète : dix blocs, un verdict global, 761 tests
backend. La branche `feat/investor-analytics` porte l'ensemble, deux PR draft l'attendent vers
`main`.

Une relecture de la page à l'écran a fait remonter des problèmes que les tests ne pouvaient pas
voir, parce qu'ils ne portent pas sur la justesse d'un calcul isolé mais sur ce que la page
**dit** et sur la place qu'elle prend :

1. **Un verdict factuellement faux.** La régularité est jugée sur des mois calendaires. Un
   investisseur qui achète strictement tous les 30 jours est noté 92 % de mois investis, CV 0,400
   et « 1 mois d'interruption » — contre 100 %, 0,000 et 0 pour le même investisseur qui achèterait
   le 6 de chaque mois. Discipline identique, sanction imméritée. Vérifié en exécutant le vrai code.
2. **Une optimisation morte.** `services/market.py:931` court-circuite l'appel réseau quand la base
   est à jour, mais compare aux **jours calendaires**, week-ends compris — or une action ne cote pas
   le samedi. La condition n'est jamais vraie : un appel Yahoo part à chaque chargement, par actif,
   pour des données déjà en base.
3. **Des ISIN partout** au lieu des noms d'actifs, alors que `MarketAsset.name` existe déjà.
4. **Trop de texte et trop de vide.** Les blocs non mesurables occupent 300 px pour dire « non », et
   les verdicts tutoient et présument (« Tu penses peut-être faire du DCA »).
5. **Le plan cible ne sait pas évoluer** : un seul montant mensuel, alors qu'un plan change quand
   les revenus changent.

M4 corrige ces cinq choses et déplace la configuration là où elle a sa place.

**Aucune migration Alembic.** Le benchmark et le plan ont déjà leurs colonnes depuis M1, le
fractionnement se déduit de la forme du JSON stocké, et le masquage est automatique — il n'y a
aucune préférence nouvelle à persister.

---

## Décisions actées avec l'utilisateur

| Sujet | Décision |
|---|---|
| Où vivent les réglages | **Onglet « Analyse » dans `/settings`**, avec un pointeur depuis `/analyse`. Le pattern existe déjà à l'identique pour Communauté (`Community.vue:272`). On n'utilise pas l'onglet « Finances » commenté : on en ajoute un propre. |
| Régularité | Jugée sur la **courbe de capital cumulé**, pas sur les mois calendaires. Aucun réglage de « mode d'investissement » : la mesure n'a pas de notion de mois, elle ne peut donc pas se tromper de convention. |
| Cadence | **Détectée et affichée** à titre descriptif (« achats espacés de 30 jours en médiane »), jamais déclarée par l'utilisateur — le §0 bis de la spec veut révéler la stratégie réelle, pas noter la stratégie déclarée. |
| Registre du texte | Constat **impersonnel**, conseil actionnable au « tu » en phrase séparée. Zéro présomption, zéro injonction gratuite. |
| Blocs non mesurables | **Repliés automatiquement** en une ligne, jamais supprimés : le message d'insuffisance est lui-même une information (« accumulateur, pas arbitragiste »). |
| Plan cible | **Fixe par défaut, fractionné en option.** Le mode se déduit de la forme du JSON, pas d'un réglage. |
| Verdict par IA | **Hors périmètre M4**, l'utilisateur le fera lui-même. Contraintes documentées en annexe. |

**Écarté explicitement** : réglage de verbosité du texte, réglage de « mode d'investissement »,
réactivation de l'onglet Finances.

---

## Global Constraints

- Deux dépôts, même branche `feat/investor-analytics`.
- Commits conventional en anglais, 2-3 lignes, pas de trailer, pas de co-author.
- Code, commentaires et docstrings en anglais ; textes utilisateur en français.
- **761 tests backend et 25 frontend au départ** (mesuré sur `feat/investor-analytics` le 2026-07-31), aucun ne doit régresser.
- Aucune dépendance nouvelle, ni back ni front.
- **Aucune migration attendue.** Si l'une devenait nécessaire : `alembic revision --autogenerate`,
  jamais d'ID écrit à la main.
- Environnement : Python 3.14.0rc2 impose de repatcher `pydantic/_internal/_typing_extra.py`
  (`prefer_fwd_module` conditionnel) dans le `.venv` après chaque `uv sync`, sinon toute la suite
  échoue au collect. Variables d'env dans `.github/workflows/ci.yml`. Front : `export PATH="/opt/node22/bin:$PATH"`.

---

## Task 1 · Le garde-fou réseau qui ne se déclenche jamais

**Files:** `services/market.py` (`_backfill_stock_prices` ~ligne 929, `_backfill_crypto_prices` ~ligne 987) ·
Test `tests/services/test_market_calendar_cache.py`

- [ ] La condition compare `set(_date_range(from_date, yesterday))` — tous les jours calendaires — à
      ce qui est en base. Les week-ends n'y sont jamais, donc `issubset` est toujours faux.
      Comparer aux **jours de bourse** (`get_non_trading_days` existe déjà, `services/market.py:67`,
      et `resolve_trading_days` en fait déjà usage dans `services/analytics/window.py`).
- [ ] Repli sûr quand le MIC est inconnu : dans le doute on appelle le réseau, on ne saute pas.
- [ ] Test : base contenant toutes les séances de la plage ⇒ **aucun appel provider** (mock, on
      vérifie `call_count == 0`) ; une séance manquante ⇒ appel effectué.
- [ ] Mesurer avant/après le nombre d'appels provider pour un chargement de `/analytics/investor`
      et l'écrire dans les résultats — c'est la seule preuve que le correctif sert.

## Task 2 · Les noms d'actifs à la place des ISIN

**Files:** `services/analytics/concentration.py`, `services/analytics/report.py`, `dtos/analytics.py` ·
Front : `CorrelationMatrix.vue`, `HoldingsSection.vue`, `PlanSection.vue`, `InvestmentPlanForm.vue`

- [ ] Résoudre `asset_key → {name, symbol}` **une seule fois** dans `report.py` (une requête
      `MarketAsset`), et faire descendre le libellé dans les DTO concernés : poids, corrélations,
      lignes écartées, dérive d'allocation.
- [ ] Afficher le **symbole** en libellé court (axes de la matrice, où la place manque) et le **nom
      complet** en infobulle. L'ISIN reste la clé technique et ne disparaît pas des `value`.
- [ ] Repli sur l'ISIN quand l'actif n'est pas connu en base — jamais de libellé vide.

## Task 3 · La régularité, jugée sur la courbe de déploiement

**Files:** `services/analytics/behaviour.py`, `services/analytics/report.py`, `dtos/analytics.py` ·
Test `tests/services/analytics/test_behaviour.py`

**Le principe.** Un DCA parfait trace une droite en capital cumulé. On mesure l'écart à cette
droite, normalisé par le capital total. Insensible aux frontières de mois par construction : ni le
rythme à 30 jours ni un achat avancé d'une semaine ne dégradent le score, alors qu'un vrai
lump-sum décroche visiblement.

- [ ] `deployment_regularity(purchases, window) -> Decimal | None` : écart moyen absolu entre la
      courbe cumulée réelle et la droite joignant premier et dernier achat, rapporté au capital
      total. 0 = déploiement parfaitement linéaire.
- [ ] **Cadence détectée**, descriptive : médiane des écarts entre achats, et dispersion du jour du
      mois. Produire un libellé du type « achats espacés de 30 jours en médiane » ou « achats autour
      du 5 du mois » selon lequel des deux est le plus resserré. Aucun réglage, aucune déclaration.
- [ ] Les indicateurs mensuels (`invested_share`, `variation_coefficient`, `longest_gap_months`,
      `temporal_hhi`, `equivalent_monthly_purchases`) **restent calculés et exposés**, mais ne
      nourrissent plus le verdict de régularité. La heatmap mensuelle reste, comme illustration.
- [ ] Le verdict de régularité se rédige sur la courbe de déploiement et la cadence détectée.
- [ ] Tests, en réutilisant les scénarios déjà écrits en scratchpad : rythme strict à 30 jours ⇒
      score quasi parfait, **et le même que** « le 6 de chaque mois » à tolérance près (c'est le
      test qui prouve que l'artefact a disparu) ; achat avancé puis mois sauté ⇒ score quasi
      inchangé ; tout le capital en un mois ⇒ score fortement dégradé.

## Task 4 · L'onglet « Analyse » dans les réglages

**Files:** Front : `src/pages/Settings.vue`, nouveau `src/components/settings/AnalyticsSettings.vue`,
`src/pages/Analysis.vue`, `src/stores/analysis.ts`

- [ ] Nouvel onglet `analyse` dans le tableau `tabs` de `Settings.vue:27-66`, à côté de Communauté.
      **Ne pas décommenter `finances`** : c'est un onglet mort dont le contenu n'est pas défini.
- [ ] `AnalyticsSettings.vue` reçoit le sélecteur d'indice et le formulaire de plan, déplacés
      depuis le haut de `/analyse`. Le composant `BenchmarkPicker.vue` est réutilisé tel quel.
- [ ] Pointeur depuis `/analyse` : un bouton dans le `PageHeader`, exactement le pattern de
      `Community.vue:272` — `<RouterLink :to="{ path: '/settings', query: { tab: 'analyse' } }">`.
      `Settings.vue` lit et resynchronise déjà `route.query.tab` dans les deux sens (lignes 67-76),
      rien à ajouter côté routeur.
- [ ] **Invalidation du cache, sinon le réglage semble ne rien faire.** Le store analytics garde
      1 h. `/analyse` doit comparer `analysis.data.benchmark_asset_key` avec
      `settingsStore.settings.benchmark_asset_key` au montage et forcer un `fetchAnalytics(true)`
      s'ils divergent. Se réparer tout seul vaut mieux que dépendre de qui a modifié quoi.
- [ ] La page `/analyse` ne contient plus que de l'analyse : verdict, blocs, notes de méthode.

## Task 5 · Le plan cible fractionné en périodes

**Files:** `services/analytics/plan.py`, `dtos/analytics.py` · Front : `InvestmentPlanForm.vue`,
`PlanSection.vue` · Test `tests/services/analytics/test_plan.py`

**Forme stockée** (dans `investment_plan_enc`, déjà chiffré, déjà existant) :

```json
{"periods": [
  {"since": "2024-01", "monthly_target": "200", "allocation": {"IE00B4L5Y983": "100"}},
  {"since": "2025-06", "monthly_target": "600", "allocation": {"IE00B4L5Y983": "80", "IE00BKM4GZ66": "20"}}
]}
```

- [ ] **Rétro-compatibilité** : la forme actuelle `{monthly_target, allocation, since}` est lue
      comme une période unique. `parse_plan` normalise les deux vers une liste de périodes.
- [ ] L'adhérence se calcule **période par période**, chaque mois complet étant confronté à la cible
      en vigueur ce mois-là. Le total agrège. La dérive d'allocation utilise la **dernière période**
      (c'est la cible actuelle qui dit ce qu'il faut rééquilibrer aujourd'hui).
- [ ] Validation par période : allocation à 100 % ± 1, montant > 0, `since` strictement croissants.
      Rejet explicite et lisible, jamais de normalisation silencieuse — comportement déjà en place
      via `PlanError`, à étendre.
- [ ] **UI : fixe par défaut.** Le formulaire montre un montant + une allocation. Une bascule
      « plan fractionné » révèle la gestion par périodes. Le mode se déduit de la présence de
      plusieurs périodes, il n'est pas stocké.
- [ ] Tests : forme ancienne toujours lue ; deux périodes ⇒ adhérence pondérée correctement par
      période ; `since` non croissants ⇒ rejet ; suppression du plan ⇒ bloc absent (déjà couvert).

## Task 6 · Le registre du texte

**Files:** `services/analytics/report.py` (une quinzaine de fonctions `_*_verdict`) ·
Test `tests/services/analytics/test_report.py`

- [ ] **Constat à l'impersonnel** : « Sur 31 mois, 24 achats. La répartition du capital équivaut à
      10,6 achats mensuels égaux. » Le sujet est le phénomène observé, pas la personne.
- [ ] **Conseil au « tu », en phrase séparée**, uniquement quand une action existe : « 22 ordres
      sont sous le seuil de rentabilité — tu peux les regrouper. »
- [ ] **Supprimé partout** : la présomption (« tu penses peut-être faire du DCA » — l'app n'a pas à
      dire à l'utilisateur ce qu'il pense), l'injonction gratuite (« ne cherche pas à corriger le
      mauvais comportement »), le ton accusateur (« ce que tu appelles régularité n'en est pas »).
- [ ] Les possessifs factuels restent : « tes frais », « ton portefeuille » — c'est du français
      normal, pas de la morale.
- [ ] L'invariant de M1 est inchangé et reste testé : un verdict ne se rédige **que** sur des
      valeurs qui ont franchi leur gate.

## Task 7 · Repli automatique et densité de la page

**Files:** Front : nouveau `src/components/analytics/CollapsibleBlock.vue`, les cinq
`sections/*.vue`, `MethodNotes.vue`

- [ ] `CollapsibleBlock.vue` : quand le bloc n'est pas mesurable, une **ligne unique** avec son
      titre et le message d'insuffisance condensé, plus un chevron pour déplier le détail des seuils
      manquants. Le repli est **automatique**, dérivé de la gate déjà calculée côté API — aucune
      case à cocher, aucune préférence stockée.
- [ ] Les verdicts par bloc passent **sous** les chiffres en petit, ou derrière un `?`. Le verdict
      global en tête reste tel quel : c'est le cœur de la page.
- [ ] Les notes de méthode gagnent une entrée sur la mesure de régularité par courbe de déploiement,
      et une sur la cadence détectée.
- [ ] Vérifier qu'un bloc replié **ne rend aucun graphe** : l'API n'envoie déjà pas les séries d'un
      bloc gaté, mais le template ne doit pas non plus monter le composant.

---

## Vérification finale

- [ ] `cd capitalview-api && uv run pytest -q` — 0 échec, les 761 tests inchangés
- [ ] `cd capitalview-web && pnpm type-check && pnpm test && pnpm build` — 0 erreur
- [ ] **Le test qui prouve M4** : rythme strict à 30 jours et « le 6 de chaque mois » obtiennent le
      même score de régularité, à tolérance près
- [ ] **Appels réseau** : chargement de `/analytics/investor` avec la base à jour ⇒ zéro appel
      provider (compté par mock), contre N+1 aujourd'hui
- [ ] **Vérification navigateur** : Postgres local + API + `pnpm dev`. Contrôler que la page ne
      porte plus que de l'analyse, que les blocs non mesurables tiennent sur une ligne et se
      déplient, que la matrice affiche des symboles et non des ISIN, que changer l'indice **depuis
      les réglages** rafraîchit bien `/analyse` au retour, et que le plan fractionné se saisit, se
      relit et se supprime.
- [ ] **Relecture du texte à l'écran** : plus aucune phrase ne présume de ce que pense
      l'utilisateur.

## Réserve reconduite de M3

La vérification navigateur tourne sur des **prix synthétiques** — le conteneur n'a pas d'accès aux
marchés. Les mécanismes sont vérifiés, les montants ne le sont pas. **Un passage sur des données
réelles reste à faire**, et il vaut plus que n'importe quelle tâche ci-dessus.

---

## Annexe — le verdict par IA (hors périmètre, pour plus tard)

L'infrastructure existe et se prête bien à l'exercice : `services/ai/agents/card_agent.py` fait
déjà rédacteur + validateur, résout le provider via `AIProviderManager.from_user_settings`, et
`services/ai/tools.py` porte un registre d'outils (`get_user_balance`,
`get_historical_performance`…) auquel un outil analytics s'ajouterait naturellement.

Trois contraintes à ne pas perdre de vue le jour où ce sera fait :

1. **L'IA est désactivée par défaut** (`ai_feature_enabled = False`, `models/user.py:97`). Le
   verdict déterministe reste le comportement de base ; l'IA l'enrichit, ne le remplace pas.
2. **L'agent ne reçoit que le payload filtré par les gates**, jamais les valeurs brutes. Un LLM à
   qui l'on donne un chiffre écrit une phrase affirmative dessus — ce qui réintroduirait à grande
   échelle le défaut corrigé en clôture de M1.
3. **Appel à la demande**, jamais bloquant au chargement : la page a déjà un poste de latence, il
   ne faut pas en ajouter un second.
