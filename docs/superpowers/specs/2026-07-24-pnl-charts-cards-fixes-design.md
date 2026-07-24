# P/L, graphes & cards — lot de correctifs

Date: 2026-07-24
Scope: `capitalview-api` (backend) + `capitalview-web` (frontend)

Cinq correctifs indépendants issus d'un retour d'usage. Chacun est autonome et
peut être implémenté/testé séparément.

---

## 1. Dividendes dans le P/L réalisé (stock, backend)

**Problème.** La card « P/L réalisé » d'un compte bourse n'affiche que les
plus/moins-values de vente. Les dividendes encaissés n'y figurent pas, alors
qu'ils sont un revenu réalisé.

**Contrainte — pas de double comptage.** Le P/L total inclut déjà les
dividendes.

Situation actuelle dans `services/stock_transaction.py` (fold des transactions) :

```
realized_profit_loss = round(realized_acc, 2)                          # ventes seules
total_profit_loss    = profit_loss_acc + realized_acc + total_dividends_acc
```

**Changement.** Replier les dividendes dans le réalisé, en laissant le total
inchangé :

```
realized_profit_loss = round(realized_acc + total_dividends_acc, 2)    # ventes + dividendes
total_profit_loss    = profit_loss_acc + realized_acc + total_dividends_acc   # inchangé
```

Invariant : `total_profit_loss == profit_loss (latent) + realized_profit_loss`.
Les dividendes apparaissent une seule fois dans `realized_profit_loss` et une
seule fois dans `total_profit_loss` → aucun double comptage.

**Portée.** Stock uniquement. Le snapshot `cumulative_pnl` (= `total_profit_loss`)
ne change pas ; la rétro-history n'a pas besoin d'être reconstruite. La card
« Dividendes » reste affichée à titre informatif.

**Pourquoi pas les récompenses crypto (staking/airdrop, type `REWARD`).**
Structurellement différent d'un dividende stock, donc **aucun traitement
équivalent nécessaire** :
- Dividende stock = versé en **cash (EUR)**, quitte la position → sans traçage
  explicite (`total_dividends`) il disparaîtrait du P/L. D'où l'inclusion.
- `REWARD` crypto = reçu **en crypto**, ajouté au portefeuille avec **coût de
  revient = 0** (cf. `enums.py`). Sa valeur est **déjà** comptée par le moteur
  de coût de revient : en **latent** tant qu'elle est détenue (valeur − 0), puis
  en **réalisé** à la vente (proceeds − 0).

Ajouter les `REWARD` dans le réalisé crypto **doublerait** le comptage. Crypto
inchangé.

**Consommateurs de `realized_profit_loss`** (vérifiés) : uniquement la card
« P/L réalisé » et sa performance dans `Stock.vue` / `Crypto.vue`. Aucun autre
code n'attend « ventes seules ». La perf réalisée (`pct(realized_profit_loss)`,
base = `total_invested`) suit automatiquement la nouvelle valeur.

**Justification financière.** « Réalisé » = argent effectivement encaissé sur
positions clôturées **+ revenus perçus**. Compter les dividendes comme réalisés
est une définition standard.

---

## 2. Retrait du sélecteur jour/semaine/mois sur le P/L journalier (frontend)

**Problème.** Sur le slide « P/L journalier » (crypto + stock), un
`BaseSegmentedControl` (Jour / Semaine / Mois / Année) est affiché mais le
graphe est câblé en dur sur `granularity="daily"`. Le contrôle ne fait donc
rien → « ça marche pas » + prête à confusion.

**Changement.** Retirer le `BaseSegmentedControl` du slot `#leading` **du seul
slide `pnl`** :
- `Stock.vue` : bloc `stockChartSlide === 'pnl'`.
- `Crypto.vue` : les **deux** blocs `chartSlide === 'pnl'` (desktop + mobile).

Le bouton de rafraîchissement (`RefreshCw`) reste. Le sélecteur est **conservé**
sur les slides `evolution` et `cumulative_pnl` où il fonctionne. Le composable
`useHistoryGranularity` n'est pas modifié.

---

## 3. Pourcentage sur le graphe P/L cumulé (frontend)

**Problème.** Le graphe « P/L cumulé » passe `absolute-performance`, ce qui
force `percent: null` dans `HistoryLineChart.visiblePerformance`. Le
`ChartPerformanceBadge` n'affiche donc que le montant € (« +555,05 € »), jamais
le pourcentage entre le début et la fin de la fenêtre visible.

**Changement.**
- Retirer la prop `absolute-performance` du `HistoryLineChart` du slide
  `cumulative_pnl` (`Stock.vue` + `Crypto.vue`) → le `%` est de nouveau calculé.
- Calcul (dans `visiblePerformance`) : `percent = (endVal − startVal) / |startVal| × 100`.
  Le dénominateur en valeur absolue garde le signe correct même si le P/L de
  départ est négatif (ex. `−100 → +50` donne `+150 %`, direction correcte).
- **Garde-fou** : si `Math.abs(startVal) < 1` (P/L de départ ≈ 0), renvoyer
  `percent: null` → seul le montant € s'affiche, pas de pourcentage aberrant.
  Remplace le `if (startVal === 0) return null` existant par ce seuil.

Ce garde-fou vit dans le chemin non-absolu de `visiblePerformance` ; il ne doit
pas affecter les autres graphes qui utilisent déjà `show-performance` sans
`absolute-performance` (`evolution`) — pour eux `startVal` est une valeur de
portefeuille, jamais ≈ 0, donc comportement inchangé.

---

## 4. Retrait des jours non cotés (weekends + fériés) des graphes actions

**Problème.** Les snapshots `account_history` sont quotidiens (continuité du net
worth). Sur les graphes actions, les weekends et fériés boursiers apparaissent
en segments plats.

**Décision.** Filtrage **par union des places réellement détenues** (pas XPAR
seul). Un jour est conservé si **au moins une** bourse du portefeuille a une
session ce jour-là. PEA (XPAR) → weekends + fériés Euronext retirés ; CTO
mixte → sessions `XPAR ∪ XNYS ∪ …` automatiquement.

**Backend** — nouvel endpoint léger, sans état :

```
GET /market/non-trading-days?from=YYYY-MM-DD&to=YYYY-MM-DD&mic=XPAR&mic=XNYS
→ { "days": ["2026-01-01", "2026-01-03", ...] }   # dates fermées pour TOUTES les MIC
```

- Utilise `exchange_calendars` (déjà dépendance, déjà utilisée dans
  `services/market.py` via `_get_calendar(mic)`).
- Pour chaque MIC valide : ensemble des sessions dans `[from, to]`
  (`calendar.sessions_in_range`). Union sur toutes les MIC.
- `non_trading_days` = tous les jours de `[from, to]` **absents** de l'union.
- MIC inconnue/non supportée → ignorée. Si **aucune** MIC valide → renvoyer
  `days: []` (⇒ le frontend ne filtre rien). **Pas** de fallback XPAR : on ne
  masque jamais un jour dont on n'est pas sûr.
- Le champ `exchange` des positions est déjà un code MIC compatible
  `exchange_calendars` (confirmé : `market.py` le passe tel quel à
  `ec.get_calendar`).

**Frontend** — sur la page Stock :
- Collecter l'ensemble des MIC **connus** distincts depuis les positions des
  comptes bourse chargés (`position.exchange`). **Aucun socle XPAR injecté** : le
  cas PEA marche naturellement (ses positions portent déjà `exchange = XPAR`).
- Si aucun MIC connu (positions non chargées, ou tous `exchange` nuls) →
  **ne pas filtrer** (afficher tous les jours). Le filtrage s'appliquera une
  fois les positions chargées.
- Appeler l'endpoint une fois pour la plage couverte par l'historique (store
  action + cache mémoire).
- Filtrer les séries des **trois** graphes actions (`evolution`, `pnl`,
  `cumulative_pnl`) en excluant les `snapshot_date` ∈ `non_trading_days` avant
  passage à `HistoryLineChart`.

**Non-objectif.** Aucune modification du stockage des snapshots : le net worth
et les autres pages restent quotidiens/continus. Le filtrage est purement une
transformation d'affichage des graphes actions.

---

## 5. Cards cliquables — même taille, sans texte d'invite (frontend)

**Problème.** Les cards cliquables (P/L, Performance) portent un
`hint: 'Appuyez pour changer'` rendu via `<p v-if="stat.hint">`. Cette ligne
supplémentaire les rend plus hautes que les cards non cliquables → grille
inégale, et le texte est jugé superflu.

**Changement.**
- Supprimer `hint: 'Appuyez pour changer'` des définitions de cards `profit_loss`
  et `performance` dans `Stock.vue` **et** `Crypto.vue`.
- Supprimer le `<p v-if="stat.hint">…</p>` du template de rendu des cards
  (`Stock.vue` + `Crypto.vue`).
- Optionnel : retirer le champ `hint` de l'interface `SummaryStatItem`
  (`useStatsPager.ts`) s'il n'a plus aucun usage après suppression.

Sans la ligne `hint`, les cards cliquables retrouvent la même hauteur que les
autres. L'affordance cliquable est conservée (`cursor-pointer` +
`hover:border-primary/60`).

---

## Ordre d'implémentation suggéré

Indépendants ; ordre par effort croissant :
1. #5 cards (pur affichage)
2. #2 sélecteur granularité P/L journalier
3. #1 dividendes réalisés (backend + test)
4. #3 pourcentage P/L cumulé
5. #4 jours non cotés (endpoint backend + test + filtrage frontend)

## Tests

- #1 : test service stock — un compte avec ventes **et** dividendes →
  `realized_profit_loss == réalisé_ventes + dividendes` ;
  `total_profit_loss == latent + realized_profit_loss` (pas de double comptage).
- #4 : test endpoint — MIC XPAR sur une plage contenant un weekend + un férié
  Euronext → ces dates présentes dans `days` ; union de deux MIC → un jour
  ouvré sur l'une seulement n'est **pas** dans `days`.
- #2, #3, #5 : vérification visuelle (front) ; pas de test unitaire dédié requis.
