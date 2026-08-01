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

- [x] La condition compare `set(_date_range(from_date, yesterday))` — tous les jours calendaires — à
      ce qui est en base. Les week-ends n'y sont jamais, donc `issubset` est toujours faux.
      Comparer aux **jours de bourse** (`get_non_trading_days` existe déjà, `services/market.py:67`,
      et `resolve_trading_days` en fait déjà usage dans `services/analytics/window.py`).
- [x] Repli sûr quand le MIC est inconnu : dans le doute on appelle le réseau, on ne saute pas.
- [x] Test : base contenant toutes les séances de la plage ⇒ **aucun appel provider** (mock, on
      vérifie `call_count == 0`) ; une séance manquante ⇒ appel effectué.
- [x] Mesurer avant/après le nombre d'appels provider pour un chargement de `/analytics/investor`
      et l'écrire dans les résultats — c'est la seule preuve que le correctif sert.

## Task 2 · Les noms d'actifs à la place des ISIN

**Files:** `services/analytics/concentration.py`, `services/analytics/report.py`, `dtos/analytics.py` ·
Front : `CorrelationMatrix.vue`, `HoldingsSection.vue`, `PlanSection.vue`, `InvestmentPlanForm.vue`

- [x] Résoudre `asset_key → {name, symbol}` **une seule fois** dans `report.py` (une requête
      `MarketAsset`), et faire descendre le libellé dans les DTO concernés : poids, corrélations,
      lignes écartées, dérive d'allocation.
- [x] Afficher le **symbole** en libellé court (axes de la matrice, où la place manque) et le **nom
      complet** en infobulle. L'ISIN reste la clé technique et ne disparaît pas des `value`.
      **Corrigé après relecture** : un ticker sur l'axe ne faisait qu'échanger un code contre un
      autre — `IWDA.AS` n'est pas plus un nom que `IE00B4L5Y983`. Les axes portent le **nom
      tronqué à 22 caractères**, l'infobulle le nom complet suivi du ticker.
- [x] Repli sur l'ISIN quand l'actif n'est pas connu en base — jamais de libellé vide.

## Task 3 · La régularité, jugée sur la courbe de déploiement

**Files:** `services/analytics/behaviour.py`, `services/analytics/report.py`, `dtos/analytics.py` ·
Test `tests/services/analytics/test_behaviour.py`

**Le principe.** Un DCA parfait trace une droite en capital cumulé. On mesure l'écart à cette
droite, normalisé par le capital total. Insensible aux frontières de mois par construction : ni le
rythme à 30 jours ni un achat avancé d'une semaine ne dégradent le score, alors qu'un vrai
lump-sum décroche visiblement.

- [x] `deployment_regularity(purchases, window) -> Decimal | None` : écart moyen absolu entre la
      courbe cumulée réelle et la droite joignant premier et dernier achat, rapporté au capital
      total. 0 = déploiement parfaitement linéaire.
- [x] **Cadence détectée**, descriptive : médiane des écarts entre achats, et dispersion du jour du
      mois. Produire un libellé du type « achats espacés de 30 jours en médiane » ou « achats autour
      du 5 du mois » selon lequel des deux est le plus resserré. Aucun réglage, aucune déclaration.
- [x] Les indicateurs mensuels (`invested_share`, `variation_coefficient`, `longest_gap_months`,
      `temporal_hhi`, `equivalent_monthly_purchases`) **restent calculés et exposés**, mais ne
      nourrissent plus le verdict de régularité. La heatmap mensuelle reste, comme illustration.
- [x] Le verdict de régularité se rédige sur la courbe de déploiement et la cadence détectée.
- [x] Tests, en réutilisant les scénarios déjà écrits en scratchpad : rythme strict à 30 jours ⇒
      score quasi parfait, **et le même que** « le 6 de chaque mois » à tolérance près (c'est le
      test qui prouve que l'artefact a disparu) ; achat avancé puis mois sauté ⇒ score quasi
      inchangé ; tout le capital en un mois ⇒ score fortement dégradé.

## Task 4 · L'onglet « Analyse » dans les réglages

**Files:** Front : `src/pages/Settings.vue`, nouveau `src/pages/settings/SettingsAnalytics.vue`
(le plan disait `src/components/settings/AnalyticsSettings.vue` ; ce répertoire n'existe pas et les
six onglets vivent tous dans `src/pages/settings/Settings*.vue` — convention du dépôt retenue),
`src/pages/Analysis.vue`, `src/stores/analysis.ts`

- [x] Nouvel onglet `analyse` dans le tableau `tabs` de `Settings.vue:27-66`, à côté de Communauté.
      **Ne pas décommenter `finances`** : c'est un onglet mort dont le contenu n'est pas défini.
- [x] `AnalyticsSettings.vue` reçoit le sélecteur d'indice et le formulaire de plan, déplacés
      depuis le haut de `/analyse`. Le composant `BenchmarkPicker.vue` est réutilisé tel quel.
- [x] Pointeur depuis `/analyse` : un bouton dans le `PageHeader`, exactement le pattern de
      `Community.vue:272` — `<RouterLink :to="{ path: '/settings', query: { tab: 'analyse' } }">`.
      `Settings.vue` lit et resynchronise déjà `route.query.tab` dans les deux sens (lignes 67-76),
      rien à ajouter côté routeur.
- [x] **Invalidation du cache, sinon le réglage semble ne rien faire.** Le store analytics garde
      1 h. `/analyse` doit comparer `analysis.data.benchmark_asset_key` avec
      `settingsStore.settings.benchmark_asset_key` au montage et forcer un `fetchAnalytics(true)`
      s'ils divergent. Se réparer tout seul vaut mieux que dépendre de qui a modifié quoi.
- [x] La page `/analyse` ne contient plus que de l'analyse : verdict, blocs, notes de méthode.

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

- [x] **Rétro-compatibilité** : la forme actuelle `{monthly_target, allocation, since}` est lue
      comme une période unique. `parse_plan` normalise les deux vers une liste de périodes.
- [x] L'adhérence se calcule **période par période**, chaque mois complet étant confronté à la cible
      en vigueur ce mois-là. Le total agrège. La dérive d'allocation utilise la **dernière période**
      (c'est la cible actuelle qui dit ce qu'il faut rééquilibrer aujourd'hui).
- [x] Validation par période : allocation à 100 % ± 1, montant > 0, `since` strictement croissants.
      Rejet explicite et lisible, jamais de normalisation silencieuse — comportement déjà en place
      via `PlanError`, à étendre.
- [x] **UI : fixe par défaut.** Le formulaire montre un montant + une allocation. Une bascule
      « plan fractionné » révèle la gestion par périodes. Le mode se déduit de la présence de
      plusieurs périodes, il n'est pas stocké.
- [x] Tests : forme ancienne toujours lue ; deux périodes ⇒ adhérence pondérée correctement par
      période ; `since` non croissants ⇒ rejet ; suppression du plan ⇒ bloc absent (déjà couvert).

## Task 6 · Le registre du texte

**Files:** `services/analytics/report.py` (une quinzaine de fonctions `_*_verdict`) ·
Test `tests/services/analytics/test_report.py`

- [x] **Constat à l'impersonnel** : « Sur 31 mois, 24 achats. La répartition du capital équivaut à
      10,6 achats mensuels égaux. » Le sujet est le phénomène observé, pas la personne.
- [x] **Conseil au « tu », en phrase séparée**, uniquement quand une action existe : « 22 ordres
      sont sous le seuil de rentabilité — tu peux les regrouper. »
- [x] **Supprimé partout** : la présomption (« tu penses peut-être faire du DCA » — l'app n'a pas à
      dire à l'utilisateur ce qu'il pense), l'injonction gratuite (« ne cherche pas à corriger le
      mauvais comportement »), le ton accusateur (« ce que tu appelles régularité n'en est pas »).
- [x] Les possessifs factuels restent : « tes frais », « ton portefeuille » — c'est du français
      normal, pas de la morale.
- [x] L'invariant de M1 est inchangé et reste testé : un verdict ne se rédige **que** sur des
      valeurs qui ont franchi leur gate.

## Task 7 · Repli automatique et densité de la page

**Files:** Front : nouveau `src/components/analytics/CollapsibleBlock.vue`, les cinq
`sections/*.vue`, `MethodNotes.vue`

- [x] `CollapsibleBlock.vue` : quand le bloc n'est pas mesurable, une **ligne unique** avec son
      titre et le message d'insuffisance condensé, plus un chevron pour déplier le détail des seuils
      manquants. Le repli est **automatique**, dérivé de la gate déjà calculée côté API — aucune
      case à cocher, aucune préférence stockée.
- [x] Les verdicts par bloc passent **sous** les chiffres en petit, ou derrière un `?`. Le verdict
      global en tête reste tel quel : c'est le cœur de la page.
- [x] Les notes de méthode gagnent une entrée sur la mesure de régularité par courbe de déploiement,
      et une sur la cadence détectée.
- [x] Vérifier qu'un bloc replié **ne rend aucun graphe** : l'API n'envoie déjà pas les séries d'un
      bloc gaté, mais le template ne doit pas non plus monter le composant.

---

## Vérification finale

- [x] `cd capitalview-api && uv run pytest -q` — 0 échec, les 761 tests inchangés
- [x] `cd capitalview-web && pnpm type-check && pnpm test && pnpm build` — 0 erreur
- [x] **Le test qui prouve M4** : rythme strict à 30 jours et « le 6 de chaque mois » obtiennent le
      même score de régularité, à tolérance près
- [x] **Appels réseau** : chargement de `/analytics/investor` avec la base à jour ⇒ zéro appel
      provider (compté par mock), contre N+1 aujourd'hui
- [x] **Vérification navigateur** : Postgres local + API + `pnpm dev`. Contrôler que la page ne
      porte plus que de l'analyse, que les blocs non mesurables tiennent sur une ligne et se
      déplient, que la matrice affiche des symboles et non des ISIN, que changer l'indice **depuis
      les réglages** rafraîchit bien `/analyse` au retour, et que le plan fractionné se saisit, se
      relit et se supprime.
- [x] **Relecture du texte à l'écran** : plus aucune phrase ne présume de ce que pense
      l'utilisateur.

---

## Résultats de l'exécution

Les onze tâches sont exécutées et poussées sur `claude/plan-m4-api-4b2xyd` dans les deux dépôts.
Aucune migration Alembic n'a été nécessaire, comme prévu, et aucune dépendance n'a été ajoutée.

- Backend **797 passed** (753 au départ de M4 dans ce conteneur, +44 tests)
- Frontend **25 passed**, `type-check` et `build` clean
- **Le test qui prouve M4** : un rythme strict de 30 jours score 0,0126 et « le 6 de chaque mois »
  0,0151 — le même déploiement, à 0,0025 près. Le verdict mensuel donnait 92 % / CV 0,400 contre
  100 % / CV 0,000 pour la même discipline.
- **Appels réseau** : sur un portefeuille de contrôle (3 lignes, 40 achats, 760 jours, historique
  complet en base), un chargement de `/analytics/investor` passe de **3 appels provider à 0** — un
  par ligne détenue, chacun un aller-retour Yahoo. C'est là que partaient les 40 s mesurées en M3.

### Vérification navigateur

PostgreSQL local, API + `pnpm dev`, portefeuille de 63 achats sur 31 mois, 3 lignes, prix
synthétiques. Contrôlé à l'écran (Playwright + captures) :

| Contrôle | Résultat |
|---|---|
| `/analyse` ne porte plus que de l'analyse | ✅ sélecteur et formulaire partis, bouton « Indice et plan cible » en tête |
| Onglet « Analyse » dans les réglages | ✅ six onglets, l'indice et le plan cible dedans |
| Blocs non mesurables repliés sur une ligne | ✅ trois blocs repliés, chacun avec sa raison ; dépliés, ils montent leur contenu |
| Un bloc replié ne rend aucun graphe | ✅ le contenu n'est pas monté tant qu'il est replié |
| Matrice de corrélation en noms | ✅ « iShares Core MSCI Wo… », « Air Liquide » sur les axes, nom complet + ticker en infobulle, aucun ISIN dans le texte de la page |
| Changer l'indice depuis les réglages | ✅ réglage persisté et repris par `/analytics/investor` au retour |
| Cycle du plan cible | ✅ allocation à 70 % refusée, plan enregistré, fractionné en 2 périodes, bloc affiché avec les deux, supprimé → bloc parti, autres blocs intacts |
| Registre du texte | ✅ aucune des tournures bannies (`tu penses`, `ne cherche pas`, `ce que tu appelles`, `jour(s)`) sur la page |
| Erreurs de page | ✅ aucune |

### Défauts trouvés pendant l'exécution et corrigés

1. **Le même garde-fou mort sur les taux de change** (`get_historical_exchange_rates_db`) : il
   exigeait lui aussi tous les jours calendaires, or le forex ne cote pas le week-end. Corrigé avec
   la même notion de jours attendus, aux jours ouvrés près.
2. **`get_non_trading_days` ne pouvait pas servir de garde** : il renvoie une liste vide aussi bien
   pour « tout est ouvert » que pour « ce MIC m'est inconnu ». Un `get_trading_sessions` distinct
   renvoie `None` dans le second cas, sans quoi un actif de place inconnue aurait sauté le réseau
   pour toujours.
3. **« Historique de 936 jours : trop court pour conclure »** sur une métrique dont l'échantillon
   dépassait largement le seuil : `Metric.gated` confondait « pas assez de données » et « valeur
   incalculable ». Deux causes, deux messages. Le repli automatique de la Task 7 a rendu ce texte
   visible en une ligne, là où il était noyé auparavant.
4. **« 14,16 € déposés n'ont jamais été investis »** était le résidu de la file FIFO, pas la
   réalité : un achat financé par une provision automatique ne consomme rien de la file, et le dépôt
   derrière lui y reste pour toujours. Le chiffre affiché est désormais dépôts moins achats — celui
   qu'un relevé confirme — et le résidu est exposé à côté, nommé pour ce qu'il est.
5. **Le pont ne disait pas à quelle date il valorisait.** L'écart de 141 € du relevé est la journée
   de marché manquante : la clôture du jour n'existe pas encore. Le bloc porte maintenant sa date.

### Task 9 : aucune garde ici — c'est une affaire de qualité de donnée

**La tâche demandait un garde-fou dans le bloc exécution. Il a été écrit, mesuré, puis retiré.**

Deux versions ont été essayées et aucune ne tient :

1. **Seuil plat en bps** (300 bps d'écart médian par ordre) : ne distingue pas un mauvais
   alignement de place d'un vrai signal de timing. Simulation : un investisseur qui achète
   systématiquement au creux d'un titre à 30 % de volatilité annualisée ressort à −483 bps
   médian avec p = 0,0002 — un signal réel et statistiquement indiscutable, que ce seuil
   supprimait purement et simplement.
2. **Fourchette cotée du mois** (prix payé dans [min, max] ± 3 %) : ne se déclenche qu'au-delà de
   5 à 8 % de décalage selon la volatilité de l'actif, alors qu'un mésalignement de place en même
   devise fait 1 à 3 %. Mesuré : sur le cas réel simulé (ETF large, décalage constant de 1,29 %),
   `part hors fourchette = 0 %`, le bloc s'afficherait normalement.

**Le seuil de 300 bps ne l'attrapait pas davantage** (écart médian mesuré à 150 bps sur ce cas) :
les deux versions ratent le cas subtil, et la première ajoute des faux positifs par-dessus.

**Décision : rien de tout ça ne vit ici.** Un prix payé et un cours stocké qui ne concordent pas,
c'est un défaut de la donnée de l'actif — il fausse le P/L, les positions et tous les graphes, pas
seulement ce bloc. Le détecter dans une métrique comportementale, c'est traiter un problème global
à un endroit local, et re-dériver un soupçon à partir de chiffres qui en sont déjà la conséquence.
La vérification appartient à la couche stock qui possède l'historique de prix, une fois, pour tout
ce qui le lit. Un commentaire en tête de `execution.py` dit pourquoi il n'y a rien.

**Le diagnostic reste entier**, et il n'a pas changé de forme : sur ta base, pour trois ou quatre
achats, comparer `price_per_unit` payé et `market_price_history.price` de cet ISIN **à cette date
exacte**. Un écart systématique de même signe sur toutes les lignes d'une même place confirme
l'hypothèse XFRA. C'est la comparaison au jour le jour — pas à la moyenne du mois — qui sépare
proprement les deux cas, indépendamment de la volatilité de l'actif : mesuré à 5 bps pour du vrai
talent contre 130 bps pour un décalage de place de 1,3 %. Si un contrôle automatique est construit
un jour, c'est cette forme-là qu'il doit prendre, côté stock.

### Audit de duplication entre analytics et les services

Après le retrait de la garde de plausibilité, la même question a été passée sur tout le module :
un fait ou une vérification qui appartient au domaine est-il redéclaré dans `analytics/` ?

**Corrigé :**

| Trouvé | Traitement |
|---|---|
| `"Provision automatique"` écrit par `stock_transaction.py:326` et **redéclaré** dans `analytics/flows.py` | La constante `AUTO_PROVISION_NOTE` vit désormais chez celui qui écrit ; analytics l'importe. Une reformulation côté stock aurait fait cesser **silencieusement** la reconnaissance des provisions — donc leur réintroduction dans les flux du MWR, l'appariement FIFO et `auto_provision_share`, tout le §0 bis. |
| Types de transaction en chaînes brutes (`"BUY"`, `"SELL"`…) dans 8 fichiers analytics, alors que `models/enums.py` porte `StockTransactionType` | Analytics importe l'enum. `StockTransactionType` hérite de `str`, donc les comparaisons existantes fonctionnent sans conversion. |
| Sentinelle de trésorerie `"EUR"` redéclarée dans 4 modules analytics, sans domicile canonique nulle part | `CASH_ASSET_KEY` créée dans `stock_transaction.py` — le service qui écrit les lignes de cash — et importée. |
| `_tx_type` (6 copies identiques) et `_tx_day` (5 copies, déjà 2 orthographes) | Regroupés dans `analytics/transactions.py`. La dérive avait déjà commencé. |

**Trouvé et délibérément non fusionné :**

`_dec` existe en deux variantes : `behaviour.py` rattrape une conversion impossible en zéro,
`counterfactual.py` la laisse remonter. Les unifier changerait un comportement dans un sens ou dans
l'autre — soit le pont avale une transaction illisible tout en continuant à « réconcilier », soit
tout l'endpoint tombe sur une donnée aberrante. La divergence est maintenant **documentée dans le
code** comme un choix, avec sa raison, au lieu d'être une dérive silencieuse.

**Reste à faire, hors périmètre M4 :**

- **Le coût moyen pondéré est réimplémenté** (`behaviour.py`) alors que `get_stock_account_summary`
  calcule déjà le sien. **Faux départ à consigner** : la duplication a d'abord été lue comme une
  divergence à corriger, parce que la couche stock met les frais dans la base de coût et que
  l'analytics ne le fait pas. Les frais ont donc été intégrés à la base de coût de la disposition —
  **puis retirés**, parce que c'était une erreur.

  Le tableau des conventions de M3 tranche **un seul axe** : coût moyen pondéré *contre FIFO*. Sur
  cet axe l'analytics était déjà conforme. La place des frais est un autre axe, que M3 n'a jamais
  posé — et sur lequel la littérature tranche dans l'autre sens. L'effet de disposition vient de la
  théorie des perspectives via Shefrin & Statman (1985) : il mesure un **point de référence
  mental**, et Odean (1998), que le §3.2 de la spec nomme comme mesure canonique, classe en
  comparant le prix de vente au **prix d'achat moyen**. Y amortir la commission transformerait un
  point de référence psychologique en seuil de rentabilité comptable — une autre grandeur.

  **Deux conventions cohabitent donc dans le bloc sorties, volontairement**, et le code le dit :

  | Mesure | Frais | Pourquoi |
  |---|---|---|
  | PGR/PLR (disposition) | **hors base de coût** | Question psychologique. Point de référence d'Odean : le prix payé par titre. |
  | Hit rate, payoff ratio | **dans les deux sens** | Question monétaire : un aller-retour qui perd après commissions n'est pas un gagnant. |

  **Et `/stock` qui annonce un P/L réalisé différent sur la même vente n'est pas une contradiction**
  — c'est la question comptable contre la question comportementale. C'était la justification
  invoquée pour le changement, et elle ne tenait pas.

  Le **partage du code** reste ouvert, mais sans urgence : les deux implémentations répondent à des
  questions différentes, donc les unifier n'est pas l'objectif. Ce qui mériterait d'être partagé,
  c'est le seul mécanisme réellement commun — le suivi de position en coût moyen pondéré — pas la
  convention de frais qui doit rester distincte de part et d'autre.
- **`stock_transaction.py` garde 29 littéraux `"EUR"` en interne.** La constante existe désormais et
  est exportée ; migrer ses propres usages est une dette préexistante, sans rapport avec analytics.

**Vérifié comme légitime :** toutes les autres portes du module sont des gates de taille
d'échantillon — « 24 achats : trop peu pour lire une habitude », « 250 rendements journaliers
communs requis », « historique de 90 jours : trop court ». C'est le cadre de fiabilité du §2 de la
spec, et c'est exactement ce qui doit rester ici. La seule gate qui n'est pas un comptage — le bloc
dépôt→achat qui se retire quand plus de la moitié des achats sont financés par provisions — **lit un
fait** (la note portée par la transaction) au lieu de deviner un défaut à partir de ses propres
chiffres. C'est la différence avec la garde supprimée.

### Limites connues, assumées

- **Toujours pas de test de composant** sur les `.vue` : décision reconduite depuis M1. L'invariant
  de la gate est garanti côté API par les tests et côté rendu par la vérification navigateur.
- **Les prix de la vérification restent synthétiques.** Les formes, les gates, les verdicts et les
  parcours sont vérifiés ; les montants ne le sont pas. La réserve de M3 tient.
- **Les blocs TWR / MWR / écart investisseur ne sont toujours pas validés** contre un calcul de
  référence indépendant. C'est le contrôle le plus lourd, et il reste à faire.
- **Le repli des taux de change sur les jours ouvrés est une approximation** : un jour férié que
  personne n'a coté coûte une requête inutile. C'est très au-dessus de l'ancien comportement, qui
  refetchait à chaque fois.
- **Le bloc plan cible est découpé en mois calendaires, pas en cadence déclarée.** La régularité
  (`deployment_gap`) est continue et lit n'importe quel intervalle sans problème — 50 jours, 90
  jours, peu importe. Mais l'adhérence au plan (`analyse_plan`) compare l'investi à la cible **mois
  par mois** : pour une cadence supérieure à 60 jours (tous les deux mois, par exemple), certains
  mois affichent 0 € et sont comptés « sous-investis » alors que le plan est tenu sur la durée — le
  ratio global reste juste, le détail mensuel devient trompeur. Non traité dans M4, signalé ici
  pour une future itération (agréger l'adhérence sur la cadence détectée plutôt que sur le mois).

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

---

# Addendum — constats sur données réelles (2026-07-31)

La page a enfin été confrontée à un portefeuille réel : PEA BoursoBank, 5 lignes (4 ETF + Air
Liquide), 9 781,75 € investis, 11 930,77 € de valeur, 77 achats sur 31 mois, 5 ventes. Jusqu'ici
toutes les vérifications tournaient sur des prix synthétiques, donc les *mécanismes* étaient
validés mais pas les *chiffres*. Ce qui suit vient de ce croisement.

## Ce qui est vérifié juste

**Les paris indépendants ne sont pas un bug.** Le soupçon initial était qu'un `N_ent` de 1,1 sur
5 lignes dont certaines corrèlent à 0,17 était trop bas. Recalcul indépendant à partir de la matrice
de corrélation et des poids réels affichés : **1,04** à volatilités égales, **1,08** à volatilités
réalistes (15-25 %). La page affiche 1,1. Le calcul est correct.

L'explication est le comportement attendu de la mesure de Meucci sur un portefeuille long-only : la
première composante principale porte 98-99 % de la variance **du portefeuille**. Les corrélations
faibles concernent des composantes auxquelles un portefeuille long sur tout n'est presque pas
exposé.

**Conséquence à écrire dans les limites, pas à corriger** : la métrique ne discrimine quasiment pas
entre deux portefeuilles actions long-only — presque tous sortiront entre 1,0 et 1,5. Elle répond à
« combien de paris distincts ? » par « un seul : les actions », ce qui est vrai et peu actionnable.
La note de méthode doit le dire, sinon l'utilisateur croit à un défaut de son portefeuille alors que
c'est une propriété de la mesure.

**Les frais sont arithmétiquement exacts** : 0,68 €/ordre × 77 = 52,39 € ; 52,39 / 10 480,86 =
0,50 % ; seuil 0,68 / 0,0025 = 272 €. Tout tombe juste.

**Le taux de rotation** (2,60 %, avec 10 481 € achetés contre 771 € vendus) se lit correctement
comme un profil d'accumulateur.

## Task 8 · Le conseil sur les frais se contredit lui-même

**Files:** `services/analytics/report.py` (`_fees_verdict`), `services/analytics/fees.py` ·
Test `tests/services/analytics/test_fees.py`

Sur le portefeuille réel, la carte affiche simultanément une tuile « coût annuel **+20 bps** » et un
texte « en dessous de 272 € par ordre tu dépasses **25 bps** de frais d'entrée — 77 de tes 77 ordres
sont sous ce seuil, **regroupe-les** ». Les deux chiffres sont justes mais le conseil est faux : la
charge totale (20 bps/an) est **déjà sous le seuil** que la page utilise pour juger.

Le seuil de 25 bps a été calibré pour un courtier à ~4,20 €/ordre. À 0,68 €/ordre, conseiller de
grouper les ordres revient à demander de casser une discipline de DCA pour économiser une
cinquantaine d'euros sur deux ans et demi.

- [x] **Ne pas émettre le conseil de regroupement quand la charge annuelle est déjà sous la cible.**
      Condition d'émission : `annual_bps > TARGET_BPS`. En dessous, le constat reste affiché (frais,
      part du capital, seuil théorique) mais sans injonction.
- [x] Reformuler pour que les deux chiffres cohabitent sans se contredire : le seuil par ordre est
      une **information de calibrage**, la charge annuelle est le **verdict**.
- [x] Test : courtier à 0,68 €/ordre avec 20 bps annuels ⇒ aucun « regroupe-les » ; courtier à
      4,20 €/ordre avec 60 bps annuels ⇒ conseil émis.

## Task 9 · Le slippage à −129 bps : suspicion de place de cotation

**Files:** `services/analytics/execution.py`, `services/market.py` (résolution du symbole) ·
Diagnostic avant correction

La page annonce **−129 bps, p = 0,000 sur 77 achats** : un achat systématiquement 1,29 % sous le
prix moyen du mois, statistiquement indiscutable. L'utilisateur confirme acheter sur les baisses,
donc le signe est plausible — **l'amplitude ne l'est pas**, et le box plot montre des moustaches de
−1000 à +1500 bps, soit 25 % d'amplitude intra-mensuelle sur des ETF larges. Ce n'est pas normal
hors krach.

**Hypothèse principale : discordance de place de cotation.** Une des lignes est référencée XFRA
(Francfort) alors que les ordres passent chez un courtier français. Si `market_price_history` a été
alimenté depuis une place différente de celle où l'ordre a été exécuté, l'écart devient systématique
et le test de permutation le certifie — il compare des jours entre eux, pas des sources entre elles.

- [x] **Diagnostiquer avant de corriger.** Pour trois ou quatre achats précis : comparer
      `price_per_unit` payé et la clôture stockée pour cet ISIN à cette date exacte. Un écart
      systématique de même signe sur toutes les lignes d'une même place confirme l'hypothèse.
- [x] Selon le résultat : soit résoudre le symbole sur la place de l'ordre, soit — si la place n'est
      pas connue — **gater le bloc exécution** quand la cohérence prix payé / prix stocké n'est pas
      établie. Un slippage faux est pire qu'un slippage absent.
- [x] Garde-fou générique, indépendant du diagnostic : si l'écart médian par ordre dépasse un seuil
      de plausibilité (à calibrer, de l'ordre de ±300 bps), traiter la série comme suspecte et
      basculer le bloc en `insuffisant` avec un caveat qui nomme la cause probable.
      **Écrit, mesuré, puis retiré** — deux calibrages essayés, aucun ne tient, et de toute façon
      ce contrôle n'a pas sa place dans une métrique comportementale. Voir les résultats.

## Task 10 · Écart de 141 € sur la valeur du portefeuille

**Files:** `services/analytics/counterfactual.py`, `services/analytics/report.py`

Le pont affiche « Toi : 11 789,20 € » contre **11 930,77 €** réels — 141,57 € d'écart, soit 1,2 %.
C'est le chiffre le plus visible de la page.

- [x] Vérifier l'hypothèse la plus probable : le pont valorise à `window.end` (hier) alors que le
      relevé est à aujourd'hui. Si c'est ça, ce n'est pas un bug mais **il faut le dire** — dater
      explicitement la valeur affichée (« au 30/07 »).
- [x] Si l'écart persiste après datation, chercher ailleurs : liquidités non comptées, ligne
      écartée du rejeu, prix manquant en fin de fenêtre.
- [x] Contrôle secondaire, même famille : le bloc dépôt→achat annonce « 14,16 € déposés n'ont jamais
      été investis » alors que dépôts − investi vaut 6,28 €. Écart faible mais l'appariement FIFO
      pourrait fuir sur les produits de cession.

## Task 11 · Deux blocs qui semblent se contredire

**Files:** `services/analytics/report.py` (`build_global_verdict`, `_bridge_verdict`)

Le verdict global ouvre sur « un robot achetant l'indice tous les mois aurait **329 € de plus** que
toi », et quelques centimètres plus bas la carte de l'écart investisseur annonce « le moment où tu
investis t'a **rapporté 165 €** ». Les deux sont exacts et mesurent des choses différentes — le
robot compare aux **actifs de l'indice**, l'écart compare à **ta propre stratégie** — mais rien ne
l'explique au lecteur, qui en conclut que la page se contredit.

- [x] Nommer la différence dans le verdict global quand les deux figures y apparaissent : le robot
      juge la **sélection d'actifs**, l'écart investisseur juge le **timing**.
- [x] Ne pas juxtaposer les deux montants sans cette précision.

## Corrections mineures confirmées à l'écran

- [x] `build_global_verdict` affiche « **1.0714** pari indépendant » — quatre décimales, alors que
      le bloc juste en dessous affiche 1,1. L'arrondi a été corrigé dans `_concentration_verdict` en
      M3 mais pas dans le verdict global.
- [x] « Ton argent est investi en médiane en **0 jour(s)** » — la forme « jour(s) » est à remplacer
      par un accord correct.
- [x] La présomption « Tu penses peut-être faire du DCA » est bien présente en production
      (déjà couverte par la Task 6).
- [x] Les ISIN bruts dans la matrice de corrélation sont bien présents en production
      (déjà couverts par la Task 2).

## Ce que ce croisement ne prouve toujours pas

Les blocs **TWR / MWR / écart investisseur** n'ont pas pu être validés : la performance globale
annoncée par le courtier (+21,97 %) est un **cumulé sur l'apport**, là où la page affiche des taux
**annualisés** (TWR 17,56 %, MWR 21,20 %). Les deux ne sont pas comparables, et leur proximité
numérique est une coïncidence. Valider ces trois chiffres demande un calcul de référence
indépendant — c'est le contrôle le plus lourd, et il reste à faire.
