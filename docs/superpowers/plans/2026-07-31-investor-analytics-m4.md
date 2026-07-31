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

- [ ] **Ne pas émettre le conseil de regroupement quand la charge annuelle est déjà sous la cible.**
      Condition d'émission : `annual_bps > TARGET_BPS`. En dessous, le constat reste affiché (frais,
      part du capital, seuil théorique) mais sans injonction.
- [ ] Reformuler pour que les deux chiffres cohabitent sans se contredire : le seuil par ordre est
      une **information de calibrage**, la charge annuelle est le **verdict**.
- [ ] Test : courtier à 0,68 €/ordre avec 20 bps annuels ⇒ aucun « regroupe-les » ; courtier à
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

- [ ] **Diagnostiquer avant de corriger.** Pour trois ou quatre achats précis : comparer
      `price_per_unit` payé et la clôture stockée pour cet ISIN à cette date exacte. Un écart
      systématique de même signe sur toutes les lignes d'une même place confirme l'hypothèse.
- [ ] Selon le résultat : soit résoudre le symbole sur la place de l'ordre, soit — si la place n'est
      pas connue — **gater le bloc exécution** quand la cohérence prix payé / prix stocké n'est pas
      établie. Un slippage faux est pire qu'un slippage absent.
- [ ] Garde-fou générique, indépendant du diagnostic : si l'écart médian par ordre dépasse un seuil
      de plausibilité (à calibrer, de l'ordre de ±300 bps), traiter la série comme suspecte et
      basculer le bloc en `insuffisant` avec un caveat qui nomme la cause probable.

## Task 10 · Écart de 141 € sur la valeur du portefeuille

**Files:** `services/analytics/counterfactual.py`, `services/analytics/report.py`

Le pont affiche « Toi : 11 789,20 € » contre **11 930,77 €** réels — 141,57 € d'écart, soit 1,2 %.
C'est le chiffre le plus visible de la page.

- [ ] Vérifier l'hypothèse la plus probable : le pont valorise à `window.end` (hier) alors que le
      relevé est à aujourd'hui. Si c'est ça, ce n'est pas un bug mais **il faut le dire** — dater
      explicitement la valeur affichée (« au 30/07 »).
- [ ] Si l'écart persiste après datation, chercher ailleurs : liquidités non comptées, ligne
      écartée du rejeu, prix manquant en fin de fenêtre.
- [ ] Contrôle secondaire, même famille : le bloc dépôt→achat annonce « 14,16 € déposés n'ont jamais
      été investis » alors que dépôts − investi vaut 6,28 €. Écart faible mais l'appariement FIFO
      pourrait fuir sur les produits de cession.

## Task 11 · Deux blocs qui semblent se contredire

**Files:** `services/analytics/report.py` (`build_global_verdict`, `_bridge_verdict`)

Le verdict global ouvre sur « un robot achetant l'indice tous les mois aurait **329 € de plus** que
toi », et quelques centimètres plus bas la carte de l'écart investisseur annonce « le moment où tu
investis t'a **rapporté 165 €** ». Les deux sont exacts et mesurent des choses différentes — le
robot compare aux **actifs de l'indice**, l'écart compare à **ta propre stratégie** — mais rien ne
l'explique au lecteur, qui en conclut que la page se contredit.

- [ ] Nommer la différence dans le verdict global quand les deux figures y apparaissent : le robot
      juge la **sélection d'actifs**, l'écart investisseur juge le **timing**.
- [ ] Ne pas juxtaposer les deux montants sans cette précision.

## Corrections mineures confirmées à l'écran

- [ ] `build_global_verdict` affiche « **1.0714** pari indépendant » — quatre décimales, alors que
      le bloc juste en dessous affiche 1,1. L'arrondi a été corrigé dans `_concentration_verdict` en
      M3 mais pas dans le verdict global.
- [ ] « Ton argent est investi en médiane en **0 jour(s)** » — la forme « jour(s) » est à remplacer
      par un accord correct.
- [ ] La présomption « Tu penses peut-être faire du DCA » est bien présente en production
      (déjà couverte par la Task 6).
- [ ] Les ISIN bruts dans la matrice de corrélation sont bien présents en production
      (déjà couverts par la Task 2).

## Ce que ce croisement ne prouve toujours pas

Les blocs **TWR / MWR / écart investisseur** n'ont pas pu être validés : la performance globale
annoncée par le courtier (+21,97 %) est un **cumulé sur l'apport**, là où la page affiche des taux
**annualisés** (TWR 17,56 %, MWR 21,20 %). Les deux ne sont pas comparables, et leur proximité
numérique est une coïncidence. Valider ces trois chiffres demande un calcul de référence
indépendant — c'est le contrôle le plus lourd, et il reste à faire.
