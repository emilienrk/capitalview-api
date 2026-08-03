# Page Analyse : réparer le contrat, puis la transformer en dashboard

## Contexte

La page Analyse souffre de deux problèmes distincts qu'il faut traiter dans cet ordre.

**Un bug silencieux.** Le commit web `e92c6e6` (31 juillet) a fait juger la régularité sur
`deployment_gap`, mais la moitié API n'a jamais été écrite. La route ayant
`response_model=InvestorAnalyticsResponse`, FastAPI supprime tout champ non déclaré : le front
lit `regularity.deployment_gap.value` sur `undefined`, la section « Ce que tu fais vraiment »
lève une `TypeError`, et Vue la remplace par un commentaire vide
(`@vue/runtime-core` : `handleError(...)` puis `result = createVNode(Comment)`). La section
disparaît sans bruit — d'où le calendrier introuvable. Vérifié en rendant réellement le
composant en SSR : payload actuel → crash ; avec le champ → la section rend entièrement,
conteneur du calendrier compris.

Un diff systématique du contrat révèle **7 champs fantômes sur 5 types**, dont trois à effet
visible : `deployment_gap` (crash), `plan.periods` (crash dès qu'un plan est déclaré) et
`fees.avoidable` (`!undefined === true` → le mauvais message s'affiche toujours).

**Un problème de lisibilité.** 30 tuiles `MetricTile`, chacune portant un badge de fiabilité et
un paragraphe de caveat. Or le même caveat est appliqué à toutes les métriques d'un bloc via un
helper `gated()` partagé : la phrase « Moins de trois ans d'historique — le signe est lisible, la
magnitude annualisée beaucoup moins. » s'affiche **6 fois** dans le seul bloc coût. C'est la
répétition à supprimer.

**Résultat visé** : une page qui ouvre sur un dashboard lisible, une phrase de verdict par bloc,
et toute la pédagogie repliée derrière un `?` — sans perdre le garde-fou qui distingue un chiffre
solide d'une estimation bruitée.

### Décision : le format tableau de bord est assumé

`docs/superpowers/specs/2026-07-29-investor-behaviour-analytics-design.md` §12 comptait parmi les
risques « la page devient un dashboard de 30 chiffres », avec pour parade « 9 métriques ».

**Cette contrainte est levée le 2026-08-03**, et la ligne du §12 a été réécrite en conséquence.
Elle avait été écrite avant que la page existe : c'était une supposition sur un mode d'échec.
La page existe maintenant, et le vrai défaut est ailleurs — non pas trop de chiffres, mais aucun
qui soit priorisé, et le même caveat répété six fois dans un bloc. Un tableau de bord hiérarchisé
est le remède, pas le risque.

Ce qui reste du §12, et qui pilote la Phase 2 : **le risque est l'absence de hiérarchie**, pas le
nombre. Il n'y a donc plus de plafond à 9 métriques à respecter. La cible est qualitative — un
étage d'accueil court, une phrase par bloc, le reste replié — et c'est le tri éditorial de la
Phase 2 qui décide de ce qui monte.

Le plan M3 (`plans/2026-07-30-investor-analytics-m3.md:19`) reprend l'ancienne formulation. Il
n'est pas modifié : c'est le compte rendu daté d'un jalon terminé, pas une règle en vigueur.

---

## Phase 0 — Réparer le contrat (bloquant)

À faire avant tout redesign : inutile de redessiner une page dont un tiers ne s'affiche pas.

### 0.1 · API — les trois champs de régularité

`capitalview-api/services/analytics/behaviour.py` : ajouter à `PurchaseRegularity` (ligne ~39)
et calculer dans `_series_regularity`, qui reçoit déjà `events` (les couples date/montant) et les
bornes de fenêtre — tout est disponible sur place.

- `deployment_gap` — écart moyen entre la courbe de capital cumulé et la droite joignant
  `(start, 0)` à `(end, total)`, rapporté au capital total. La méthode est déjà décrite mot pour
  mot dans `MethodNotes.vue`, y compris le plancher d'environ `1/(2n)` dû aux ordres discrets.
- `cadence_label` — descriptif, jamais déclaré : le plus resserré entre le jour du mois et
  l'intervalle médian. `median_day_of_month` et `day_of_month_spread` existent déjà et servent de
  base.
- `median_gap_days` — intervalle médian entre achats.

`services/analytics/report.py` (`_regularity_payload`) : exposer les trois. `deployment_gap`
passe par le helper `gated()` local, comme les autres métriques du bloc.

`dtos/analytics.py` (`RegularityResponse`) : déclarer les trois champs, sinon FastAPI les
supprime — c'est précisément le bug d'origine.

### 0.2 · API — les trois autres champs à effet visible

- `PlanResponse.periods` — le front fait `plan.periods.length` (`PlanSection.vue:52`) : crash.
- `FeesResponse.avoidable` — le front fait `!fees.avoidable` (`FeesSection.vue:50`) : affiche
  aujourd'hui systématiquement la mauvaise branche.
- `DepositLagResponse.unpaired_deposits_eur` — note jamais affichée.

`counterfactual.valued_at` et `regularity.median_gap_days` sont déclarés côté TS mais inutilisés
dans les composants : les traiter en même temps, sans urgence.

### 0.3 · Un filet contre la quatrième occurrence

C'est la troisième fois que le front devance l'API. Deux niveaux :

- **Maintenant, sans dépendance** : un test de rendu SSR par section, avec des fixtures
  construites depuis les DTO. `@vue/server-renderer` est déjà livré avec `vue` et tourne sans
  DOM — j'ai validé l'approche pendant le diagnostic. Il faut stubber `localStorage`
  (`usePrivacyMode` le lit à l'import) et `window.location` (`api/client.ts`), et importer le
  composant dynamiquement pour que les stubs précèdent l'import. Ça attrape les crashs.
- **Durable, à décider séparément** : générer les types TS depuis l'`openapi.json` de FastAPI
  plutôt que de les écrire à la main. Ça supprime la classe de bug entière. Contrainte réelle :
  les deux repos sont séparés, donc aucun test mono-repo ne peut diffuser le contrat — il faut
  soit committer le schéma dans le repo web, soit le récupérer en CI.

---

## Phase 1 — La fiabilité passe au second plan

Aujourd'hui `MetricTile.vue` rend un `ReliabilityBadge` sous **chaque** tuile, badge + phrase.
Le fichier porte cette règle volontairement seule (« the rule lives in exactly one file on
purpose ») : le changement est donc concentré.

Nouveau comportement :

| Fiabilité | PC | Mobile |
|---|---|---|
| `solide` | rien | rien |
| `indicatif` / `insuffisant` | marqueur discret, **survol du chiffre** → la phrase | marqueur discret, **tap** → la phrase |

Un chiffre solide n'a plus rien sous lui : c'est l'attente par défaut. Le marqueur ne subsiste
que quand la donnée est dégradée, parce que c'est là qu'il protège d'une lecture fausse.

Deux primitives à créer, aucune n'existe :

- `src/components/base/BaseTooltip.vue` — survol sur pointeur fin, tap sur tactile. Il n'y a
  aujourd'hui aucun Tooltip ni Popover dans `src/components/base/`.
- `src/composables/useMediaQuery.ts` — suivre le motif `matchMedia` déjà employé dans
  `useDarkMode.ts`.

Fichiers touchés : `MetricTile.vue`, `ReliabilityBadge.vue`.

---

## Phase 2 — Une phrase par bloc

Trois coupes, par ordre de gain :

1. **Le caveat partagé** — hissé au niveau du bloc, affiché une fois quand au moins une métrique
   est dégradée, au lieu d'être répété sous chaque tuile. C'est ce qui supprime les 6 occurrences
   du bloc coût.
2. **Les paragraphes d'illustration** — par exemple « Les chiffres mensuels ci-dessus illustrent,
   ils ne jugent pas… » (`BehaviourSection.vue`) : déplacés dans le `?` de la Phase 3.
3. **Le verdict** — conservé, une phrase, sous les chiffres. Il est dynamique, calculé en Python
   à partir des vrais montants (`_regularity_verdict`, `_concentration_verdict`…) : c'est le
   produit, pas du remplissage.

Ajout en tête de page : le verdict global existant (`VerdictBanner`) suivi d'une rangée compacte
de 4–5 chiffres phares. C'est l'étage « dashboard ».

**Le tri des 30 tuiles.** Répartition actuelle : `BehaviourSection` 12, `FeesSection` 8,
`CostSection` 4, `HoldingsSection` 3, `PlanSection` 3 — les deux premières sont donc où la coupe
porte. Pour chaque tuile, trois sorts : promue en chiffre phare de l'étage d'accueil, gardée dans
son bloc, ou rétrogradée dans le `?`.

Aucun plafond chiffré ne s'impose (cf. la décision du 2026-08-03 ci-dessus) : ce tri est un choix
éditorial sur ce qui mérite l'attention, pas une décision technique. À faire avec toi, tuile par
tuile.

---

## Phase 3 — Le `?` par bloc

`MethodNotes.vue` (176 lignes, `<details>` en bas de page) contient déjà tout le « comment c'est
calculé ». Le problème n'est pas qu'il manque, c'est qu'il est loin de ce qu'il explique.

Éclater son contenu et le rattacher au bloc concerné, derrière un bouton `?` en tête de bloc.
Chaque panneau contient : définitions des termes, méthode de calcul, et les caveats en entier.
La section fourre-tout en bas de page disparaît, sauf la partie « ce que cette page ne calcule
pas », qui n'appartient à aucun bloc et reste globale.

---

## Vérification

- **Le bug d'origine** : `capitalview-web` → `npm test`. Le test SSR de la Phase 0.3 doit rendre
  `BehaviourSection` sans lever, et le HTML doit contenir le conteneur du calendrier
  (`touch-action:pan-y`).
- **API** : `pytest tests/services/analytics tests/routes/test_analytics_routes.py`. Attention,
  la suite complète compte **24 échecs préexistants** liés à l'environnement Python local
  (identiques avant/après tout changement) — c'est le sous-ensemble analytics qui fait foi.
- **Contrat** : re-jouer le diff DTO ↔ types TS ; il doit renvoyer zéro champ fantôme.
- **À l'œil, sur un vrai téléphone** : c'est le seul moyen de valider les Phases 1 à 3. Je ne
  peux pas lancer l'app ici (elle demande l'API et un état connecté avec tes données), donc les
  choix de marges et de survol restent à confirmer visuellement.
