# Handoff — Analyse comportementale investisseur, jalon M1

**Date de l'arrêt :** 2026-07-30
**Pour :** l'agent qui reprend cette session
**Branches poussées :** `feat/investor-analytics` dans `capitalview-api` **et** `capitalview-web` (deux repos git distincts, mêmes noms de branche)

Lis ce document en entier avant de toucher au code. Il contient tout ce que la session précédente savait et que le ledger ne dit pas.

---

## 0. Comment on est arrivés là

L'utilisateur a demandé une analyse comportementale de son investissement boursier (2 ans d'historique PEA/CTO). Déroulé :

1. **Brainstorming** (skill `superpowers:brainstorming`) → design doc complet :
   `docs/superpowers/specs/2026-07-29-investor-behaviour-analytics-design.md`
   Ce doc définit **9 métriques réparties en 3 tiers**, dont une seule (l'écart investisseur MWR−TWR) est construite dans ce jalon M1. Lis-le si tu as besoin du "pourquoi" derrière une métrique — le plan d'implémentation ne le répète pas.

2. **Deux corrections importantes de l'utilisateur pendant le brainstorming**, déjà intégrées au design doc, mais capitales pour comprendre le code :
   - **« Achat ≠ dépôt »** (§0 bis du design doc) : la discipline d'investissement se mesure sur les **achats**, jamais sur les dépôts. Un dépôt erratique suivi d'un achat systématique en fin de mois, c'est un investisseur discipliné — pas l'inverse. Conséquence directe dans le code : `include_auto_provisions` (voir §2).
   - **Prix bruts + dividendes saisis manuellement** : ce modèle de données existant est correct et volontaire, on n'y touche pas. Le seul vrai défaut trouvé est que le backfill Yahoo utilise `auto_adjust=True` par défaut (prix ajusté dividendes) alors que le cron nocturne stocke du brut — mais ça ne joue que sur les actifs **distribuants**. Le benchmark est donc contraint à être un ETF **capitalisant** (`IE00B4L5Y983`, iShares Core MSCI World Acc), ce qui rend le problème nul pour lui. **Hors périmètre, pas corrigé, pas à corriger dans ce jalon.**

3. **Plan écrit** (skill `superpowers:writing-plans`) pour le jalon M1 seul (pas M2/M3) :
   `docs/superpowers/plans/2026-07-29-investor-analytics-m1.md`
   8 tâches. Auto-révision faite avant dispatch (code mort supprimé, un test qui aurait flappé corrigé, deux instructions vagues rendues concrètes).

4. **Exécution** via `superpowers:subagent-driven-development` : un agent implémenteur par tâche, une revue par tâche, boucle de correction si besoin. C'est là qu'on s'est arrêtés.

---

## 1. État exact — À VÉRIFIER TOI-MÊME AVANT TOUTE ACTION

Ne fais confiance à personne sur ce point, vérifie :

```bash
cd capitalview-api  && git log --oneline main..feat/investor-analytics && git status --short
cd capitalview-web  && git log --oneline main..feat/investor-analytics && git status --short
```

Au moment de l'écriture de ce document :
- `capitalview-api` @ `3877215`, arbre propre, poussé sur `origin/feat/investor-analytics`
- `capitalview-web` @ `9fd75c3`, arbre propre, poussé sur `origin/feat/investor-analytics`
- Suite backend : **609 passed, 0 failed** (dernière exécution complète, commit `3877215`)
- Suite frontend : 25 passed (dernière exécution connue, commit `9fd75c3` — avant l'ajout de Task 5 côté API, ce qui ne la concerne pas)

## 2. Tableau d'avancement des 8 tâches du plan M1

| # | Tâche | Code | Tests | Revue | Statut réel |
|---|---|---|---|---|---|
| 1 | Cadre de fiabilité (`reliability.py`) | ✅ `2cd6d00` | ✅ | ✅ propre | **TERMINÉE** |
| 2 | Flux externes / refactor R1 (`flows.py` + `account_history.py`) | ✅ `37c8742`+`073df73` | ✅ | ✅ propre (2 findings corrigés, mutation-vérifiés) | **TERMINÉE** |
| 3 | TWR + XIRR (`returns.py`) | ✅ `e5d6ed4`+`4610d59` | ✅ 609 dans la suite globale | ✅ propre (voir §3 — arbitrage important) | **TERMINÉE** |
| 4 | Réglages benchmark + plan (`user_settings`) | ✅ `c317ddc`+`37c1a94` | ✅ | ⚠️ **PAS DE RE-REVUE** après le fix — voir §4 | **CODE FAIT, RE-REVUE À LANCER** |
| 5 | Série benchmark (`benchmark.py`) | ✅ `3877215` | ✅ 4/4, suite 609 | ❌ **AUCUNE REVUE LANCÉE** | **CODE FAIT, PREMIÈRE REVUE À LANCER** |
| 6 | Assemblage + endpoint (`report.py`, DTOs, route) | ❌ | ❌ | ❌ | **PAS COMMENCÉE** |
| 7 | Store + types frontend (`stores/analysis.ts`) | ✅ `4f1a9e1`+`9fd75c3` | ✅ | ✅ propre | **TERMINÉE** |
| 8 | Page `/analyse` | ❌ | ❌ | ❌ | **PAS COMMENCÉE** — un dispatch a été tenté et est mort sur une erreur serveur 529 avant d'écrire quoi que ce soit. Le repo web est intact, aucune trace résiduelle à nettoyer. |

**Prochaine action concrète, dans l'ordre :**
1. Lancer la revue de Task 4 sur `4610d59..37c1a94` (le fix du contrôleur, voir §4)
2. Lancer la revue de Task 5 sur `3877215` (jamais revue)
3. Dispatcher Task 6, puis Task 8
4. Revue finale de branche complète (`superpowers:requesting-code-review`, modèle le plus capable)
5. `superpowers:finishing-a-development-branch`

## 3. Un arbitrage technique important que tu dois connaître — Task 3

Le plan M1 (écrit par la session précédente) contenait un test dont les données étaient incohérentes avec son propre nom (`test_twr_skips_days_with_a_non_positive_base...` fournissait un cas où la base était positive). Le premier implémenteur a résolu ça en élargissant la condition de skip à `previous_value <= 0 or base <= 0`.

**Le contrôleur a annulé ce choix et restreint la condition à `base <= 0` seul.** Raison : sauter tous les jours qui *ouvrent* à zéro fait perdre un vrai rendement (un compte financé à 500€ qui clôture le même jour à 550€ a un vrai +10%, en convention Modified Dietz journalière start-of-day). C'était un défaut dans les **données de test du plan**, pas dans l'implémentation.

**Cet arbitrage a été confirmé indépendamment par un reviewer dédié** (raisonnement de méthodologie standard, pas seulement "je fais confiance au contrôleur"). Voir `services/analytics/returns.py::time_weighted_return`, la garde `if base <= _ZERO:`. **Ne pas revenir dessus sans relire tout `.superpowers/sdd/2026-07-29-investor-analytics-m1/progress.md` autour de "Task 3".**

## 4. Un défaut Critical trouvé et corrigé — Task 4, migration Alembic

Le plan avait inventé l'ID de révision Alembic `a1b2c3d4e5f6` sans vérifier qu'il était libre. **Il collisionnait avec une migration existante déjà committée** (`a1b2c3d4e5f6_add_position_to_notes.py`, au milieu de la chaîne). Deux fichiers avec le même `revision` cassent `alembic upgrade`/`history`/`heads` pour **tout le repo**, pas juste cette migration.

Trois tentatives de dispatch pour corriger ont échoué sur des erreurs serveur 529 consécutives. **Le contrôleur a fait le correctif lui-même** (renommage mécanique) plutôt que de continuer à brûler des tentatives : `revision = "a1b2c3d4e5f6"` → `"9c4f1ab73e20"`, fichier renommé via `git mv`, `down_revision` inchangé. Vérifié statiquement (script Python dans le ledger, sous "Task 4: fix round 1/5") : 0 doublon, 0 branche, une seule tête = la nouvelle migration. Suite complète relancée : 605 passed à ce moment-là.

**Ce correctif n'a jamais eu de revue formelle** (ni initiale — le contrôleur l'a fait directement — ni scoped re-review). C'est la toute première chose à faire en reprenant : générer le package de revue sur `4610d59..37c1a94` avec `scripts/review-package`, et dispatcher un reviewer dessus avant de considérer Task 4 comme terminée.

**Leçon pour le reste du plan (M2/M3) :** si tu dois inventer un ID de révision Alembic dans un futur plan, grep `revision` ET `down_revision` sur `alembic/versions/*.py` avant de l'écrire dans le plan — l'erreur vient d'avoir seulement vérifié `down_revision`.

## 5. Comment reprendre — mécanique SDD

Tout est dans `capitalview-api/.superpowers/sdd/2026-07-29-investor-analytics-m1/` (gitignoré, c'est le workspace de travail de la skill, normal qu'il ne soit pas dans les commits) :

- `progress.md` — **le ledger, source de vérité**. Sa première ligne nomme le plan. Chaque tâche a une ligne "complete" ou l'historique de ses rounds de correction. Lis-le en entier, il est plus détaillé que ce document.
- `task-N-brief.md` — l'énoncé exact extrait du plan pour chaque tâche, déjà généré pour les tâches 1-5, 7, 8.
- `task-N-report.md` — rapport de l'implémenteur, avec les rounds de fix ajoutés à la suite.
- `review-*.diff` — packages de diff déjà générés pour les revues passées.

Pour relancer la skill : invoque `superpowers:subagent-driven-development` avec ce plan :
`docs/superpowers/plans/2026-07-29-investor-analytics-m1.md`. Elle détectera le ledger existant et **doit reprendre à la première tâche sans ligne "complete"**, pas repartir de zéro — c'est exactement pour ça que le ledger existe.

Rappel de contrainte du plan : **deux repos git séparés**, `capitalview-api` (tâches 1-6) et `capitalview-web` (tâches 7-8), même nom de branche dans les deux. Ne jamais lancer deux agents implémenteurs en parallèle sur le même repo (conflits d'index git) — mais un agent API et un agent web en parallèle sont sans risque, ils ne partagent aucun fichier.

## 6. Modèle et effort

Session précédente : agent principal en Opus/xhigh, subagents en Opus/medium (demande explicite de l'utilisateur). **Le `/model` a été changé en Sonnet 5 juste avant cette pause** — c'est une commande locale de l'utilisateur, pas une décision de l'agent. Décide en reprenant si tu gardes Opus pour les subagents ou si tu suis le nouveau défaut ; note que Task 5 est passée sans souci en Sonnet quand Opus était indisponible (529 répétés), donc Sonnet est un choix raisonnable pour les tâches de pure transcription — le plan contient déjà tout le code.

## 7. Ce que M1 ne construit pas (rappel du plan)

Le pont contrefactuel, le coût d'exécution, la régularité des achats, le décalage dépôt→achat, le conditionnement au marché, les paris indépendants, les frais, l'effet de disposition, et le bloc plan cible sont tous **hors scope de M1** — ils arrivent en M2/M3, non planifiés à ce jour. Le formulaire de saisie du plan cible n'existe pas non plus ; seul le champ de stockage (`investment_plan_enc`) a été ajouté en Task 4.
