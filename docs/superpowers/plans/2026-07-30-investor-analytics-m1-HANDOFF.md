# Handoff — Analyse comportementale investisseur, jalon M1 : **CLÔTURÉ**

**Dernière mise à jour :** 2026-07-30
**Branches :** `feat/investor-analytics` dans `capitalview-api` **et** `capitalview-web`

> Ce document a d'abord servi à reprendre une session interrompue. M1 est terminé depuis ;
> il est conservé comme trace de clôture. Le plan exécuté est
> `docs/superpowers/plans/2026-07-29-investor-analytics-m1.md`, dont la section
> « Vérification finale » porte le détail des contrôles.

---

## 1. État final

Les 8 tâches du plan M1 sont codées, testées, revues et poussées.

| # | Tâche | Statut |
|---|---|---|
| 1 | Cadre de fiabilité (`reliability.py`) | Terminée |
| 2 | Flux externes / refactor R1 (`flows.py` + `account_history.py`) | Terminée |
| 3 | TWR + XIRR (`returns.py`) | Terminée |
| 4 | Réglages benchmark + plan (`user_settings`) | Terminée, revue faite |
| 5 | Série benchmark (`benchmark.py`) | Terminée, revue faite |
| 6 | Assemblage + endpoint (`report.py`, DTOs, route) | Terminée |
| 7 | Store + types frontend (`stores/analysis.ts`) | Terminée |
| 8 | Page `/analyse` | Terminée |

Suites : backend **616 passed / 0 failed** ; frontend **25 passed**, `type-check` clean, build OK.

## 2. Ce que les revues de clôture ont trouvé

**Task 4 — collision d'ID Alembic : correctif confirmé.** La chaîne complète a été reparsée en
tenant compte des deux formes de déclaration (`revision = "..."` et `revision: str = '...'`) — la
vérification d'origine ne couvrait que la première et ne voyait donc qu'un tiers des fichiers.
Résultat sur les 33 migrations : aucun ID dupliqué, aucun point de branche, aucun parent manquant,
une racine, **une seule tête** (`9c4f1ab73e20`). Rejeu complet sur PostgreSQL vierge : OK, plus
`downgrade`/`upgrade` aller-retour.

**Task 5 — `benchmark.py` : rien à signaler.** Le point vérifié était la docstring « prix EUR » :
`_backfill_stock_prices` convertit bien via des taux de change historiques par date, donc
`MarketPriceHistory.price` est en EUR et l'affirmation est exacte.

**Refactor R1 — équivalence prouvée.** `day_txs` est déjà filtré par `tx.executed_at.date() == d` ;
`stock_external_flow_for_day` réapplique le même prédicat. Le filtre ajouté est un no-op strict :
les snapshots sont inchangés par construction, pas seulement en pratique.

**Task 6 — un défaut trouvé et corrigé.** Le `_verdict` était rédigé à partir des valeurs *brutes*
`gap`/`gap_eur` au lieu des valeurs filtrées par la gate. Avec un historique court, toutes les
métriques basculaient bien en `insuffisant` et affichaient `—`, mais la page servait quand même une
phrase affirmative (« Ta stratégie fait mieux que toi… 0 € ») construite sur des chiffres que la
gate venait de juger non affichables — le mode d'échec exact que §2 de la spec existe pour empêcher.
Le verdict lit désormais les métriques filtrées.

## 3. Limites connues, assumées

- **Pas de test de composant sur `Analysis.vue`.** Le dépôt web n'a ni `@vue/test-utils` ni jsdom, et
  le plan interdit toute nouvelle dépendance en M1. L'invariant « une métrique `insuffisant`
  n'affiche jamais de nombre » est donc garanti côté API par les tests, et côté rendu seulement par
  la vérification navigateur manuelle décrite dans le plan. À réévaluer en M2.
- **Le contrôle des courbes `/stock` est un argument de code, pas un contrôle visuel.** Voir §2.
- **`benchmark_asset_key` et `investment_plan` ne peuvent pas être remis à `null`** via `PUT
  /settings` (garde `if data.X is not None`). Sans conséquence en M1 : aucun formulaire ne les
  expose encore. À traiter quand le formulaire de plan cible arrivera (M3).

## 4. Environnement — piège connu

Le conteneur de développement n'a que **Python 3.14.0rc2**, alors que pydantic 2.12.5 appelle
`typing._eval_type(..., prefer_fwd_module=True)`, paramètre qui n'existe qu'à partir de 3.14 final.
Toute la suite backend échoue au collect. `uv python install 3.14` ne propose que rc2. Contournement
appliqué dans le `.venv` uniquement (non versionné, à refaire dans un conteneur neuf) : ne passer le
kwarg que si `typing._eval_type` l'accepte. Les tests ont aussi besoin des variables d'environnement
de `.github/workflows/ci.yml` (`SECRET_KEY`, `DATABASE_URL`, `ENCRYPTION_KEY`…).

## 5. Suite

M2 et M3 n'existent qu'à l'état d'une ligne de tableau chacun au §10 de la spec — **aucun document
de plan n'a été écrit pour eux**. M2 couvre le pont contrefactuel (§1.2) et le coût d'exécution
(§1.3) ; il introduit la première dépendance à numpy, que M1 s'interdisait. Écrire le plan M2 est un
travail préalable à part entière.
