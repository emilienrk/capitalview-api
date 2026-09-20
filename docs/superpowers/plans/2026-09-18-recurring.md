# Abonnements : détection robuste — Plan d'implémentation (API)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** reconnaître chaque charge récurrente (streaming, forfait, énergie, assurance, loyer, sport, crédit, logiciel…) dans les opérations bancaires, **sans être dérouté** par un libellé qui change, un prix qui bouge, un mois sauté, une pause, un remboursement, un rejet, un changement de compte ou de moyen de paiement ; en tirer « dont abonnements » dans le Réel, un onglet Abonnements et des questions Oui / Non là où le doute reste.

**Remplace** la tâche R5 (et la table `bank_subscriptions` de R1) du plan `2026-09-16-real-cashflow-types-subscriptions.md`. Ce plan couvre **l'API** ; la partie web (W) est esquissée en fin de plan et sera détaillée ensuite.

**Tech Stack :** FastAPI + SQLModel + Alembic + pytest (`capitalview-api`).

**Prototype mesuré** (local, jamais committé) : `capitalview-api/.claude/subscriptions-prototype/` — `extract.py` (dump via le vrai pipeline), `proto2.py` (l'algorithme ci-dessous, en `float`), `stability.py` (rejeu à chaque fin de mois), `synthetic.py` (29 cas limites). Mode d'emploi dans son `README.md`. Il sert d'oracle pendant l'implémentation : mêmes entrées, mêmes séries.

---

## Décisions à valider avant de coder

Chacune a une recommandation ; les mesures qui la fondent sont plus bas.

1. **Les abonnements sûrs sont comptés sans question** (recommandé). Sûr = prélèvement avec ≥ 6 échéances régulières, ou carte avec ≥ 12 échéances au centime. Marqués « Détecté », « Ce n'est pas un abonnement » à un clic. Un virement n'est **jamais** sûr (loyer, argent de poche, épargne ailleurs se ressemblent). Amende la décision du 2026-09-16 « détectés puis confirmés ». Sur le dump : 6 sûrs, **9 questions au lieu de 15**. Alternative : tout passe par une question (15).
2. **Un abonnement peut couvrir plusieurs comptes** (recommandé) : Olness payé par virement Revolut (2022-2023) puis carte et prélèvement Boursorama (2023-2025) est **un** abonnement à deux épisodes, pas deux séries (amende « Olness : 2 comptes = 2 séries »).
3. **Loyer Frederic Durand** (380 €/mois, 2025-07 → 2026-06) : typé **Neutre** par une règle du 2026-09-16, il n'est **pas** proposé — un abonnement est une marque sur une Dépense. Garder la règle, ou la passer en Dépense ?
4. **Crédits d'un marchand abonné** (régularisations EDF +44,15 € et +39,26 €, trop-perçus Bouygues +9,33 € et Orange +6,22 €, clôture COM AIR +50 €, Allianz +45,65 €) : 6 crédits, 194,61 €, **tous sous le seuil de 100 €** des questions de flux, donc comptés aujourd'hui en Entrées. Recommandé : affichés avec l'abonnement, et un crédit lié à un abonnement compté **pose sa question de flux quel que soit son montant**, « Remboursement » proposé (+6 questions sur le dump) ; jamais typé d'office (« Remboursement : une réponse, pas un type »).
5. **« X € à confirmer » ne compte pas les questions d'abonnement** (recommandé) : y répondre ne change ni les entrées ni les dépenses, seulement « dont abonnements ». Elles restent dans le badge, la file « À trier » (montant affiché : coût annuel) et le filtre « à vérifier ».

## Définitions

- **Abonnement** : toute charge **récurrente**, dépense (`EXPENSE`) — y compris loyer, énergie, assurance, crédit. Pas un type : une marque sur des dépenses. Les dépenses l'incluent ; le Réel affiche « dont abonnements ».
- **Occurrence régulière** : un débit qui tombe à son échéance. **Extra** : un débit du même marchand rattaché à l'abonnement hors échéance (prorata d'entrée ou de sortie, régularisation, débit en retard, double prélèvement). **Annulée** : une occurrence dont le remboursement ou le rejet est apparié (`refund`, `reversal`) — elle compte pour le rythme, jamais pour le montant.
- **Épisode** : suite d'échéances sans trou de plus de `max_missed` échéances ; une pause ouvre un nouvel épisode du **même** abonnement.
- **Niveau de prix** : suite d'au moins 2 occurrences au même montant — ±1 % (5 centimes au moins), ±3 % pour une série dont les montants flottent avec le change (moins de 60 % de ses pas à ±1 %) ; son montant est celui de sa dernière occurrence. Un montant isolé entre deux niveaux n'est pas un changement de prix (prorata, régularisation).
- **Confiance** : `certain` (compté sans question, décision 1) ; `probable` (question Oui / Non) ; en dessous, rien n'est proposé (l'utilisateur peut toujours marquer une opération lui-même).

## Mesures qui fondent le plan (dump réel du 2026-09-18)

4 264 opérations, 4 comptes (Principal et Secondaire courants, Livret A, LDDS), 53 mois ; **3 088 débits éligibles** (finals, montant > 0, hors jambes de virement interne, hors retraits), **758 marchands** après identité, **353 flux** candidats.

**Résultat : 15 abonnements, 0 faux positif.** Tous les « proposés » validés le 2026-09-16 sont trouvés sauf le loyer Frederic Durand (décision 3) — MACIF en **un** abonnement (un seul débit par mois : les « 2 contrats » du 16/09 étaient deux libellés), Olness aussi (décision 2) ; aucun des « jamais proposés » (SNCF, Carrefour, Leclerc, Lidl, Food Star/King/Gare, `da easy`, McDonald's, Burger King, laverie, `VIR INST <nom>`) ne sort.

| Confiance | Abonnement | Cadence | Montant actuel | Période | Occ. | Ce qui le rendait difficile |
|---|---|---|---|---|---|---|
| certain | EDF | mensuel | 60,00 € | 2023-10 → 2026-06 (en retard) | 29 + 5 | renommé `ELECTRICITE DE FRANCE` → `EDF clients particuliers` sans mot commun ; 55 → 57,30 → 58,55 → 60 € (la hausse de 2,2 % de juin 2025 comprise) ; régularisation 112,81 € ; nouveau format bancaire 2026-06 ; jour du mois passé du 5 au 16 |
| certain | MACIF | mensuel | 72,96 € | 2024-10 → actif | 23 | **5 changements de prix** (17,76 → 28,31 → 115,69 → 119,97 → 62,44 → 72,96, au centime près d'un mois à l'autre) ; juin 2025 absent ; libellé allongé en 2026-07 |
| certain | Olness | mensuel | 39,00 € | 2022-10 → 2025-05 | 24 + 4 | 2 comptes, 3 moyens de paiement (virement Revolut, carte, prélèvement) ; pause de 6 mois ; entité HTML dans le libellé (`OLNESS.apos"`) |
| certain | Bouygues Telecom | mensuel | ≈ 17 € (variable) | 2024-09 → actif | 10 | montant variable ; **15 mois de pause** ; format du libellé changé |
| certain | Orange | mensuel | ≈ 12 € (variable) | 2025-01 → 2026-04 | 11 + 1 | variable (0,70 à 85,25 €) ; 5 mois de pause |
| certain | COM AIR | mensuel | ≈ 32 € (variable) | 2025-11 → 2026-04 | 7 | dernière échéance **rejetée** (`REJ PRLV`) ; +50 € de clôture |
| probable | Loyer Transalp'Dome | mensuel | 530,00 € | 2026-06 → actif | 3 | **juillet absent** ; 2 formats de libellé ; dépôt de garantie 1 000 € (Neutre) le même jour sous un 3ᵉ |
| probable | Allianz Direct (carte) | mensuel | 4,02 € | 2026-03 → actif | 7 | **renommé** `AllSecur` → `Allianz Direct` ; 2ᵉ abonnement Allianz le même jour |
| probable | Allianz Direct (prélèvement) | mensuel | 5,81 € | 2026-07 → actif | 3 | 1ʳᵉ échéance 12,31 € ; parallèle au précédent |
| probable | Fitness Park (Arverne) | **4 semaines** | 30,00 € | 2026-05 → actif | 5 | 13 débits par an, pas 12 ; 35 € puis 10 € au démarrage |
| probable | Anthropic Claude | mensuel | 21,60 € | 2026-07 → actif | 3 | achat ponctuel 5,21 € chez le même marchand en mars |
| probable | Pathé Cinépass | mensuel | 16,90 € | 2024-09 → 2025-03 | 5 + 1 | 1ᵉʳ paiement par carte (17 €), puis prélèvement ; dernier au prorata (14,15 €) ; billets Pathé Annecy à côté |
| probable | OVH | mensuel | ≈ 7,79 € (variable) | 2026-02 → 2026-05 | 4 + 1 | **renommé** `OVH SAS` → `OVHcloud` ; deux débits en mars |
| probable | EA (Electronic Arts) | mensuel | 5,99 € | 2025-10 → 2026-02 | 4 | décembre absent, puis reprise à un autre jour |
| probable | Supercell | mensuel | 5,39 € | 2022-11 → 2023-03 | 3 | Revolut (moyen de paiement inconnu) ; 2 mois absents |

« Occ. » : occurrences régulières + extras.

- **Actifs aujourd'hui** : 7 abonnements, **≈ 684 €/mois** d'équivalent mensuel, dont le loyer (530 €) ; Fitness Park compte pour 30 × 13 / 12.
- **« Dont abonnements »** (occurrences typées Dépense, si les 15 sont confirmés) : 192 € (2022), 478 € (2023), 1 276 € (2024), 2 111 € (2025), 3 114 € (2026 à fin août ; loyer depuis juin).
- **Ce que les approches précédentes donnaient** (plan du 2026-09-16) : signature seule → 14 séries, manquaient loyer, Orange, Bouygues, MACIF ; chaînage sur un mot commun → bruit (`food`, `le`, `da easy`, SNCF, Leclerc, McDo).
- **Rejets examinés un par un** (338 flux) : habitudes (CROUS 307 passages, Carrefour 158, SNCF 102, Lidl, Burger King, McDo, BlaBlaCar), petits achats à prix fixe (laverie, bus, cafétéria, distributeur), virements à des proches, recharges IZLY. Deux vrais abonnements rejetés en cours de mise au point l'ont révélé : COM AIR (dernière échéance rejetée typée Neutre → le porteur est la dernière occurrence **non annulée**) et OVH (variable par carte, bruit de date de comptabilisation → écart toléré 2,5 j).
- **Faux positifs corrigés en route** (chacun a donné une règle) : `TOTAL` (carburant) avalé par `TotalEnergies` (préfixe approximatif → ≥ 4 lettres **et** ≥ 50 % du mot long) ; `COM AIR` fusionné avec un vol `AIR …` (`com` n'est pas un mot vide) ; Lidl (~3 passages par mois : une chaîne mensuelle régulière existe toujours dans des visites hebdomadaires → exclusivité sur les seules occurrences régulières) ; `ESPRIT THAI` 19,99 € → `JULES` 19,99 € pris pour un renommage (renommage seulement depuis une série à montant fixe au centime) ; deux additions identiques dans un pub à un an d'écart (annuel à 2 occurrences : exclusivité et date à ±7 j).
- **Stabilité dans le temps** (`stability.py`, détection rejouée à chaque fin de mois de 2025-03 à 2026-09) : chaque abonnement garde son identité d'un mois à l'autre (MACIF de 6 à 23 occurrences sans jamais se couper malgré 5 hausses, EDF de 17 à 29), aucun trou, la confiance ne fait que monter (`probable` → `certain`). Un nouvel abonnement mensuel apparaît à sa 3ᵉ échéance (Anthropic, loyer, Allianz 5,81 € en 2026-09). Seul faux positif transitoire : `VIR INST ROUKINE EMILIEN` (virements vers ses propres comptes, 3 montants ronds identiques en 2025) — neutralisé par la règle « pas de question d'abonnement tant que la question de flux du libellé est ouverte ».
- **Cas limites synthétiques** (`synthetic.py`, 29 scénarios) : les 24 positifs sont trouvés avec la bonne cadence et le bon type de montant, les 5 témoins négatifs (supermarché 3×/semaine, boulangerie 1,20 € tous les 2 jours, cantine en semaine, virements aléatoires à un ami, recharges rondes) ne donnent rien. Liste en Tâche S2.
- **Performance du prototype** (Python, `float`, sans base) : préparation ≈ 36 ms, détection ≈ 170 ms, lecture ≈ 14 ms pour 3 088 débits. Reconstruction actuelle des patterns : ~320 ms. Budget : **+200 ms au plus** sur la reconstruction, qui ne tourne que quand les données changent.
- **Dates** : pour une carte, la comptabilisation arrive 0 à 6 jours après l'achat (1 à 3 le plus souvent) ; `transaction_date` n'existe que sur les opérations récentes (212 cartes sur 2 047). La cadence lit `transaction_date` quand elle existe, sinon la date retenue ; les tolérances absorbent le reste. Le libellé (« CARTE 02/07/26 ») n'est **jamais** lu pour dater.
- **Libellés** : Boursorama allonge ses libellés depuis 2026-06 (`… ROUKINE EMILIEN Numero de client : 602600413`, `… RUM MA02`, `… Virement pour le loyer de Emilien R Réf : SCT…`). Les signatures (`label_signature`) changent ; l'identité marchand (Tâche S1) non.

## L'algorithme

Neuf couches, chacune rattrapant ce que la précédente laisse passer. Tout est **pur** sauf le chargement et le stockage.

### 1. Entrée

Débits **finals**, montant > 0, pas une jambe de virement interne (`savings`, `recurring`, `learned`, `confirmed`), pas un retrait d'espèces (`WITHDRAWAL`). Une jambe `refund` / `reversal` reste, marquée **annulée**. Chaque débit porte : compte, date (`transaction_date` sinon date retenue), montant, devise, moyen de paiement (`operation_type`), **type résolu** (`flows._filed`) et marchand. La détection tourne **par devise** (deux devises ne se comparent jamais). Les crédits non appariés servent seulement à lier des remboursements (couche 9).

Le type ne sert pas à trouver les séries (un loyer retypé Neutre à moitié reste une série), mais à décider de ce qu'on en fait : une série est proposable si son **porteur** (dernière occurrence non annulée) est une Dépense et si ≥ 50 % de ses occurrences sont des Dépenses ; seules les occurrences typées Dépense sont comptées.

### 2. Identité marchand (`services/banking/merchants.py`)

- **Mots** d'un libellé : suites de lettres ≥ 2 caractères, casse et accents repliés (NFKD), une suite contenant un chiffre écartée **entière** (comme `type_rules.telling_words`), ordre conservé, doublons retirés, moins `NOISE` = `label_groups.NOISE_WORDS` ∪ { `prelev`, `numero`, `client`, `sct`, `vers`, `ech` (nouveau format), `sarl`, `sas`, `sasu`, `eurl`, `sa`, `sca`, `cie`, `ltd`, `gmbh`, `inc`, `llc`, `srl`, `bv`, `ag`, `plc` (formes juridiques), `apos`, `amp`, `quot` (entités HTML), `www`, `http`, `https`, `et`, `en`, `au`, `aux` }. **Pas** `com` ni `fr` : `COM AIR` en a besoin. Aucun mot → clé = libellé nettoyé (`label_groups.display_label`), comparée à l'identique.
- **Poids** d'un mot : `ln((N + 1) / (df + 1)) + 1`, `df` = nombre de clés distinctes (débits et crédits de l'utilisateur) qui le contiennent. Un mot partout (« roukine ») pèse peu, un nom de marchand beaucoup.
- **Mots équivalents** : égaux ; ou l'un préfixe de l'autre avec ≥ 4 lettres **et** ≥ 50 % de la longueur de l'autre (`electronic ar` ~ `electronic arts`, mais pas `total` ~ `totalenergies` ni `ovh` ~ `ovhcloud`) ; ou distance de Damerau-Levenshtein ≤ 1 dès 5 lettres (faute de frappe, troncature).
- **Même marchand** si Jaccard pondéré ≥ 0,6, **ou** contenance pondérée (poids partagé / poids du plus court) ≥ 0,75 **et** premiers mots équivalents. La contenance absorbe un libellé qui s'allonge ; l'exigence du premier mot empêche « Annecy » d'avaler « Carrefour Annecy ».
- Groupes par union-find, en ne comparant que les clés qui partagent les 4 premières lettres d'un mot. **Tous comptes et moyens de paiement confondus** (décision 2).

### 3. Cadences

| Cadence | Pas | Tolérance | Échéances manquées tolérées | Points minimum | A priori | Par an |
|---|---|---|---|---|---|---|
| hebdomadaire | 7 j | ±2 j | 2 | 6 | — | 52 |
| bimensuelle | 14 j | ±3 j | 2 | 5 | — | 26 |
| 4 semaines | 28 j | ±3 j | 2 | 5 | — | 13 |
| mensuelle | 1 mois | ±6 j | 2 | 2 | +0,6 | 12 |
| bimestrielle | 2 mois | ±8 j | 1 | 3 | — | 6 |
| trimestrielle | 3 mois | ±10 j | 1 | 3 | +0,1 | 4 |
| semestrielle | 6 mois | ±15 j | 1 | 2 | — | 2 |
| annuelle | 12 mois | ±20 j | 1 | 2 | +0,3 | 1 |

- Un pas se mesure en **mois fractionnaires** pour les cadences calendaires : `année × 12 + mois − 1 + (jour − 1) / jours du mois`, calculé une fois par opération. Un débit du 31 janvier suivi du 28 février fait exactement un mois ; le 29 février passe tout seul. Cadences en jours : sur les numéros de jour.
- Un pas de `n` échéances (`n ≤ 1 + manquées`) est accepté si son écart au pas attendu ≤ tolérance + `(n − 1) × max(1, tolérance // 2)`.
- **4 semaines contre mensuel** : un flux mensuel dont ≥ 4 écarts consécutifs tombent entre 27 et 29 jours est à 4 semaines (Fitness Park : 28, 28, 28, 28). Moins de 5 points ne suffisent jamais à dire « 4 semaines » : deux dates à 56 jours d'écart le sont par hasard bien plus souvent (loyer de juin, août, septembre).

### 4. Flux dans un marchand (programmation dynamique)

- Pour une cadence, meilleure chaîne de points par date : chaque pas vaut `+1 − 0,35 × (échéances sautées) − 0,4 × (écart / (tolérance + 1))²`, moins une pénalité de montant. On retient, toutes cadences confondues, la chaîne au meilleur score + a priori, on retire ses points, on recommence.
- **Passe A — montant fixe** : seuls les pas « plats » (écart ≤ max(3 %, 0,10 €) ; le 3 % couvre le change d'un abonnement en devise), pénalité `0,2 × écart relatif` ; chaînes d'au moins 3 points. Trouve Amazon Prime au milieu des achats Amazon, chaque niveau de prix de MACIF, deux abonnements Apple sous un même libellé.
- **Passe B — montant libre**, sur ce que A laisse : tout pas autorisé, pénalité `0,25 + 0,5 × min(1, |ln(b / a)| / ln 2)` quand le montant change (rien s'il est plat) ; chaînes d'au moins 2 points. Une série est **variable** si moins de la moitié de ses pas sont plats (≥ 3 points).
- **Élagage** : un marchand de ≥ 30 débits à plus de 2,5 débits par échéance ne peut pas être un seul flux à cette cadence (cantine, boulangerie) : la cadence est sautée ; une cadence plus longue que deux fois l'étendue du marchand aussi.

### 5. Raccords dans un marchand

- **Bout à bout** (trié par premier point) : un flux en prolonge un autre s'ils ne se chevauchent pas, avec des cadences compatibles (identiques, ou mensuelle / 4 semaines), et : trou ≤ `1 + manquées + 0,5` échéances (**changement de prix** ou saut), **ou** même montant, **ou** l'un est variable (**pause** : nouvel épisode du même abonnement, quelle que soit sa durée).
- **Flux faible chevauchant un fort** (régularisation EDF 112,81 €, doublon) : il devient des **extras** du fort — seulement si le marchand est **dédié** au fort (occurrences régulières ≥ 75 % des débits du marchand sur sa période ± une échéance) et s'il n'est pas lui-même propre (fixe avec au moins un pas au centime, ≥ 2 points). Sinon il reste un flux à part : un second abonnement du même marchand (Allianz 4,02 € carte et 5,81 € prélèvement le même jour), ou des achats (rejetés plus loin).
- Après raccords, la règle 4 semaines est réappliquée à la série entière.

### 6. Renommages entre marchands

- **Passage de relais** : une série S2 prolonge S1 (marchands différents) si S1 est à montant fixe avec ses deux dernières occurrences au centime, **même compte**, même famille de moyen de paiement (prélèvement / virement / carte + inconnu), cadences compatibles, S2 commence après la fin de S1 à ≤ `1 + manquées + 0,3` échéances, sans chevauchement, et **la 1ʳᵉ occurrence de S2 égale la dernière de S1 au centime**. Retrouve `ELECTRICITE DE FRANCE` → `EDF` (57,30 €), `AllSecur` → `Allianz Direct` (4,02 €).
- **Nouveau nom vu une seule fois** : un débit isolé (marchand de ≤ 2 débits), même compte, même moyen de paiement, **même montant au centime**, exactement à l'échéance suivante d'une série d'au moins 3 occurrences, et seul candidat → rattaché (`OVH SAS` → `OVHcloud`, 7,79 €). Une série variable n'y a droit que si ce montant n'est pas rond.
- Rien d'autre : `Orange` → `Bouygues` (autre montant, opérateur changé) ou `EDF` → `TotalEnergies` (déménagement) restent deux abonnements ; l'utilisateur peut les fusionner (Tâche S5).

### 7. Extras

Pour une série dont le marchand est **dédié** : débits Dépense non rattachés du même marchand, entre `premier − (une échéance + tolérance)` et `dernier + (1 + manquées) × échéance + tolérance`, de montant entre 0,1 et 4 fois la médiane des occurrences régulières : 1ᵉʳ paiement par carte (Pathé 17 €), prorata de sortie après un mois sauté (Pathé 14,15 €), débit en retard, double prélèvement (Olness avril 2025). Ils sont comptés comme dépenses d'abonnement et montrés comme « hors échéance ».

### 8. Lecture : confiance, statut, prix

Caractéristiques de chaque série : occurrences régulières `n`, **couverture** (occurrences / échéances attendues dans les épisodes), **écart médian** au pas (jours), part des pas plats et au centime, moyen de paiement majoritaire, montant rond (entier), **exclusivité** (occurrences régulières / débits du marchand sur la période, moins ceux d'une **autre** série fixe propre du même marchand), part de Dépenses, part d'annulées.

Confiance, dans cet ordre :

1. **Refus** : porteur absent ou pas une Dépense, < 50 % de Dépenses, ≥ 50 % d'annulées (`LW - YAPLA` : chaque paiement remboursé).
2. **Annuel ou semestriel à 2 occurrences** : prélèvement à ±10 % (révision de prime), écart ≤ 15 j, exclusivité ≥ 0,5 ; autre moyen : montant au centime, non rond, ≥ 10 €, écart ≤ 7 j, exclusivité ≥ 0,7 → `probable`.
3. **Prélèvement** : `certain` si `n ≥ 6`, couverture ≥ 0,75, écart ≤ 3 j ; `probable` si `n ≥ 2` et au moins 3 débits en comptant les extras, couverture ≥ 0,6, écart ≤ 5 j. Un mandat existe : le montant peut varier.
4. **Virement** : montant fixe (≥ 75 % de pas plats) obligatoire ; `probable` si `n ≥ 3`, couverture ≥ 0,6, écart ≤ 5 j. **Jamais** `certain`.
5. **Carte ou moyen inconnu** : hebdomadaire et bimensuel refusés (habitudes). Variable : seulement mensuel ou 4 semaines, exclusivité ≥ 0,75, couverture ≥ 0,8, et (`n ≥ 4`, écart ≤ 2,5 j) ou (`n ≥ 3`, écart ≤ 1 j). Fixe : ≥ 60 % de pas au centime (ou ≥ 90 % plats si non rond), écart ≤ 3 j, couverture ≥ 0,6, exclusivité ≥ 0,7 ; `n ≥ 3`, porté à 6 sous 2 €, et à 5 avec écart ≤ 1 j pour un montant rond (paris, recharges, tickets). `certain` seulement si `n ≥ 12`, ≥ 90 % au centime, écart ≤ 2 j, couverture et exclusivité ≥ 0,9.

**Statut** (calculé à la lecture, jamais stocké : il dépend du jour) : prochaine échéance = dernière occurrence régulière + une cadence ; délai de grâce = max(5 j, un quart de cadence). **À jour au …** si le compte n'est connu que jusqu'à une date (`covered_until` : dernière synchronisation d'un compte lié, dernière opération d'un compte importé, comme `real_cashflow._coverage_gaps`) antérieure à l'échéance + grâce et à aujourd'hui − 3 j — un livret non synchronisé ne « finit » pas ses abonnements. Sinon **actif** si aujourd'hui ≤ échéance + grâce, **en retard** jusqu'à `(1 + manquées) × cadence + tolérance` après la dernière occurrence, **terminé** au-delà.

**Montant actuel** : montant du dernier niveau (fixe) ou médiane des 3 dernières occurrences non annulées (variable). **Changements de prix** : entre niveaux successifs seulement (avant → après, date, %). **Coût annuel estimé** = montant actuel × occurrences par an ; **payé sur 12 mois** = somme réelle (extras compris, annulées exclues).

### 9. Décisions et corrections de l'utilisateur

- Une décision (`bank_subscriptions`) garde des **ancres** : `hash_index(uuid)` des occurrences de la série au moment de la décision (JSON chiffré). À chaque reconstruction, elle se rattache à la série détectée qui partage le plus d'ancres ; à défaut (opérations réimportées sous d'autres uuid), à celle dont l'**identité** stockée (mots marchand, comptes, cadence, montant) correspond : même marchand, un compte commun, cadence compatible, montant à ±25 %. Une série porte au plus une décision (la plus récente) ; une décision peut couvrir plusieurs séries (fusion, ou série coupée par une évolution de l'algorithme).
- Une nouvelle occurrence d'un abonnement confirmé est marquée d'office : elle appartient à la même série. Un refus est retenu de même.
- **Corrections** (ce que l'algorithme rate) : marquer une opération comme abonnement (graine : la reconstruction cherche autour d'elle dans son marchand et son compte, sans minimum de points ; une seule occurrence suffit pour un annuel vu une fois) ; rattacher ou détacher une opération (`includes` / `excludes`, prioritaires sur la détection) ; fusionner deux abonnements (Orange → Bouygues si l'utilisateur le veut) ; renommer ; forcer la cadence ; oublier la décision.
- **Remboursements liés** : un crédit non apparié, non annulé, du même marchand (couche 2), sur n'importe quel compte, entre une échéance avant le début et 4 mois après la fin. Montrés avec l'abonnement ; comptés en moins dans « dont abonnements » **seulement** si l'utilisateur les a typés Dépense (« Remboursement »). Lié à un abonnement compté, il pose sa question de flux même sous `FLOW_QUESTION_MIN_AMOUNT`, avec « Remboursement » proposé (décision 4).
- **Questions** : une série `probable` sans décision porte `subscription_question` sur son porteur, **sauf** si le groupe de flux du porteur a encore sa question de flux ouverte (la question de flux passe d'abord : un virement vers soi-même se range en Épargne ou Neutre et sort des candidats).

## Cas limites couverts

| # | Cas | Traitement | Où |
|---|---|---|---|
| 1 | Libellé qui s'allonge (nouveau format, RUM, n° client, réf.) | contenance pondérée + même premier mot | S1 |
| 2 | Renommage sans mot commun (`AllSecur` → `Allianz Direct`, `ELECTRICITE DE FRANCE` → `EDF`, `OVH SAS` → `OVHcloud`) | passage de relais au centime, même compte, échéance suivante | S2 (§6) |
| 3 | Moyen de paiement qui change (carte → prélèvement, virement instantané → permanent) | mots de plomberie bancaire ignorés, marchand commun | S1, S2 |
| 4 | Changement de compte (Revolut → Boursorama) | marchand tous comptes confondus | S1, S2 |
| 5 | Hausse ou baisse de prix, plusieurs fois (MACIF ×5) | niveaux de prix raccordés bout à bout | S2 (§5) |
| 6 | Prix d'appel, essai à 1 €, 1ʳᵉ échéance au prorata | extras au premier créneau ; montant nul exclu à l'entrée | S2 (§1, §7) |
| 7 | Montant variable (énergie, téléphone à l'usage, hébergement) | passe B, règles par moyen de paiement | S2 (§4, §8) |
| 8 | Régularisation, double prélèvement, débit en retard | extras d'un marchand dédié | S2 (§5, §7) |
| 9 | Remboursement carte (`AVOIR`) apparié à une échéance | occurrence annulée : rythme gardé, montant exclu | S2 (§1) |
| 10 | Prélèvement rejeté (`REJ PRLV`) puis représenté | annulée ; le porteur est la dernière non annulée | S2 (§8) |
| 11 | Mois sauté (loyer de juillet, EA en décembre) | pas de `n` échéances toléré | S2 (§3) |
| 12 | Pause, suspension, reprise des mois après (Bouygues 15 mois) | nouvel épisode du même abonnement | S2 (§5) |
| 13 | Abonnement saisonnier (salle fermée l'été) | épisodes ; « payé sur 12 mois » réel à côté de l'estimation | S2 (§8) |
| 14 | 4 semaines contre mensuel (Fitness Park) | ≥ 4 écarts de 27-29 j, ≥ 5 points | S2 (§3) |
| 15 | Hebdo, bimensuel, bimestriel, trimestriel (eau), semestriel, annuel (assurance) | table des cadences ; annuel à 2 occurrences | S2 (§3, §8) |
| 16 | Fin de mois (31 → 28), année bissextile, week-end et jours fériés | mois fractionnaires, tolérances | S2 (§3) |
| 17 | Délai de comptabilisation carte (0-6 j) | `transaction_date` d'abord, tolérances | S2 (§1) |
| 18 | Deux abonnements sous un libellé (Apple 2,99 € + 9,99 €), deux lignes au même prix, deux contrats le même jour | passe A par niveau, flux parallèles, exclusivité hors autre série propre | S2 (§4, §8) |
| 19 | Abonnement caché chez un marchand fréquent (Prime parmi les achats Amazon) | passe A ; pas d'extras chez un marchand non dédié | S2 (§4, §7) |
| 20 | Habitude à prix fixe (cantine, boulangerie, laverie, bus, café) | élagage de densité, exclusivité, règles carte | S2 (§4, §8) |
| 21 | Virements à des proches, argent de poche, virements vers soi | virement jamais `certain` ; question de flux d'abord | S2 (§8), S4 |
| 22 | Dépôt de garantie le même jour que le loyer | type Neutre : ni extra ni occurrence comptée | S2 (§1, §7) |
| 23 | Série retypée par l'utilisateur (loyer Frederic Durand en Neutre) | proposable seulement si le porteur et ≥ 50 % sont des Dépenses | S2 (§8) |
| 24 | Paiement en 4 fois, crédit | détectés ; jamais `certain` sous 6 échéances : une question | S2 (§8) |
| 25 | Abonnement en devise (change qui fluctue) | détection par devise, pas plat à 3 % ; totaux en devise principale seulement | S2 (§1, §4) |
| 26 | Opération en attente | hors détection ; « à venir » l'exclut si elle correspond à l'échéance | S8 |
| 27 | Compte non synchronisé, historique qui commence tard | statut « à jour au … » ; premier débit proche du début de couverture → « depuis au moins … » | S7 |
| 28 | Coïncidence de montant chez un autre marchand (`ESPRIT THAI` → `JULES` 19,99 €) | renommage seulement depuis une série fixe au centime, même moyen, marchand successeur de ≤ 2 débits | S2 (§6) |
| 29 | Mots génériques (`air`, `total`, `annecy`), formes juridiques, entités HTML, préfixes de processeur (`PAYPAL *`, `SUMUP *`, `NYX*`) | poids par rareté, préfixe ≥ 4 lettres et ≥ 50 %, mots vides, premier mot | S1 |
| 30 | Contrat remplacé (EDF → TotalEnergies après déménagement) | deux abonnements ; fusion manuelle possible | S2 (§6), S5 |
| 31 | Décision après réimport, fusion ou coupure d'une série | ancres HMAC puis identité | S5 |
| 32 | Compte supprimé | décisions élaguées ; séries disparues avec les opérations | S3 |
| 33 | Abonnement résilié encore prélevé, prix qui augmente, doublon | données prêtes (`ended_on`, niveaux, extras) ; alertes après ce plan | S7 |
| 34 | Performance sur un long historique | calcul dans la reconstruction, empreinte de fraîcheur, élagages | S4 |
| 35 | Confidentialité | tout chiffré ; ancres = HMAC chiffrés ; rien de joignable en clair avec `bank_transactions` | S3 |

## Global Constraints

- **Branche** : `feat/subscriptions` depuis `main` (API maintenant, web plus tard).
- **Migration** : une seule, neuve, `down_revision = '7b2e5f9c3a41'`.
- **Dérivé** dans `bank_transfer_patterns` (JSON chiffré, reconstruit quand l'empreinte change) : `transfer_patterns._VERSION` → `"10"`, `ledger.LEDGER_VERSION` → `"4"`. **Décisions** dans `bank_subscriptions`, dans l'empreinte (nombre, `max(updated_at)`).
- **Outillage local jamais committé** : `routes/dev_debug.py`, `scripts/debug_db.py`, `tests/routes/test_dev_debug.py`, le montage du routeur de debug dans `main.py`, `.claude/` (dont le prototype). Committer par chemins explicites ; `git status --short` avant et après.
- Tout chiffré (`encrypt_data`) ; recherches par `hash_index`. **Aucune valeur jointable en clair avec `bank_transactions`** dans une table portant `user_uuid_bidx` : les ancres sont des `hash_index(uuid)` **dans** un JSON chiffré.
- Aucun libellé lu pour dater ni pour chiffrer un montant ; le moyen de paiement ne décide que du niveau de confiance.
- Commentaires anglais, rares, le *pourquoi* (les mesures de ce plan dans les constantes). Conventional commits anglais, **3 lignes max**, aucune mention d'outil ou d'assistant.
- `uv run pytest -q` (sandbox désactivé) ; **mutation sur chaque test neuf**, base verte d'abord.
- Rejeu sur le dump : `.claude/subscriptions-prototype/README.md` ; **lecture seule**, dump supprimé après usage.

---

# Partie S — API

## Tâche S1 — Identité marchand (pur)

**Fichiers :** nouveau `services/banking/merchants.py`, nouveau `tests/services/test_banking_merchants.py`.

- [x] `merchant_words(label) -> tuple[str, ...]`, `NOISE` (§2), repli sur `label_groups.display_label` sans mot.
- [x] `Idf(keys)`, `words_alike(a, b)` (préfixe ≥ 4 lettres et ≥ 50 %, Damerau-Levenshtein ≤ 1 dès 5), `same_merchant(a, b, idf)`, `group_merchants(keys) -> dict[key, int]` (union-find, seaux par 4 premières lettres ; ordre déterministe).
- [x] Tests (mutation sur chacun), libellés réels :
  - même marchand : `PRLV SEPA EDF clients particuliers` / `… ROUKINE EMILIEN Numero de client : 602600413 1526162A1I1G1SD` ; `VIR SEPA TRANSALP'DOME S.A.S.` / `… Virement pour le loyer de Emilien R Réf : SCT40618202608030071849` / `VIR INST TRANSALP DOME S A S` ; `To Sarl Olness'` / `CARTE 08/11/23 OLNESS' CB*8897` / `PRLV SEPA OLNESS-OLNESS.apos"` ; `VIR INST FREDERIC DURAND` / `VIR SEPA Frederic Durand` ; `CARTE 13/03/26 ANTHROPIC CB*0837` / `CARTE 02/07/26 ANTHROPIC* CLAUDE CB*0837` ; `EA *ELECTRONIC AR` / `EA *ELECTRONIC ARTS` ;
  - marchands différents : `CARTE TOTAL 4` / `PRLV SEPA TotalEnergies Electricite et G…` ; `PRLV SEPA COM AIR` / `CARTE AIR FRANCE` ; `CARTE PATHE ANNECY 2` / `PRLV SEPA Pathe CinePass` ; `ANNECY` / `CARREFOUR ANNECY` ; `OVH SAS` / `OVHcloud` ; `ELECTRICITE DE FRANCE` / `EDF clients particuliers` (ces deux derniers : rattrapés en S2, pas ici) ;
  - un mot présent dans toutes les clés pèse moins qu'un mot rare ; libellé sans mot → comparé à l'identique ; groupes identiques quel que soit l'ordre d'entrée.

## Tâche S2 — Récurrence (pur)

**Fichiers :** nouveau `services/banking/recurrence.py`, nouveau `tests/services/test_banking_recurrence.py`.

- [x] Entrée `RecurrenceOp(id, account, day, amount: Decimal, currency, method: OperationType, type: CashflowType, cancelled, merchant: int)` ; sortie `Series(cadence, regular, extras, variable, merchants, links)` où `links` dit chaque raccord (`price`, `gap`, `pause`, `rename`, `rename_once`) et sa date.
- [x] §3 : `Cadence` (table), coordonnées (numéro de jour, mois fractionnaire) calculées une fois, `step(cadence, a, b) -> (échéances, écart) | None`, règle 4 semaines.
- [x] §4 : `best_chain`, `extract` (passe A fixe ≥ 3 points, passe B libre ≥ 2), pénalités, élagage de densité.
- [x] §5 : raccords bout à bout, flux faibles en extras chez un marchand dédié.
- [x] §6 : passage de relais, nouveau nom vu une fois.
- [x] §7 : extras.
- [x] §8 : `features`, `confidence`, `levels`, `episodes`, montant actuel, coût annuel, prochaine échéance ; `status(series, today, covered_until)` à part (lecture).
- [x] Détection par devise ; ordre des sorties déterministe (même entrée → mêmes séries, quel que soit l'ordre des lignes).
- [x] Tests — les 29 scénarios de `synthetic.py`, un test chacun (mutation sur chacun) :
  - trouvés : hausse 13,49 → 15,99 € avec délai carte 0-3 j (2 niveaux) ; renommage `AllSecur` → `Allianz Direct` ; un mois sauté ; deux mois de suite ; un mois remboursé (annulée, 12 occurrences dont 11 comptées) ; pause de 5 mois (2 épisodes) ; Apple 2,99 € + 9,99 € sous un libellé, jours différents puis même jour (2 séries) ; deux lignes au même prix (2 séries) ; Prime annuel 69,90 € parmi des achats Amazon aléatoires (annuel, sans extras) ; assurance annuelle à 2 occurrences 119 → 124,50 € ; virement permanent hebdomadaire (probable) ; énergie variable 40-90 € le 5 (certain, variable) ; eau trimestrielle variable ; 4 × 62,50 € puis rien (probable, terminé) ; crédit 350 € × 24 (certain) ; change 10,05-10,34 € (fixe) ; le 31, 30, 28 (mensuel) ; salle à 29,99 € tous les 28 j (4 semaines) ; argent de poche bimensuel par virement (probable) ; essai 1 € puis 11,99 € ; carte puis prélèvement ; changement de compte ; libellé qui s'allonge ;
  - rien : supermarché 3×/semaine ; boulangerie 1,20 € tous les 2 j ; cantine 3,30 € en semaine ; virements aléatoires à un ami ; recharges de 20 € presque mensuelles.
- [x] Tests ciblés (mutation) : `step` (31 janv. → 28 févr. = 1 mois ; 29 févr. ; 3 échéances sautées refusées) ; `ESPRIT THAI` 19,99 € → `JULES` 19,99 € non rattaché ; deux additions de pub identiques à un an d'écart refusées ; flux Lidl (11 passages réguliers parmi 34) refusé ; porteur = dernière non annulée (COM AIR) ; ≥ 50 % d'annulées refusé ; série à moitié Neutre refusée ; exclusivité d'Apple 2,99 € non pénalisée par Apple 9,99 € ; 4 semaines exige ≥ 5 points (loyer juin/août/septembre reste mensuel) ; un raccord ne chevauche jamais.

## Tâche S3 — Schéma, export, purge, suppression de compte

**Fichiers :** nouvelle migration, `models/banking.py`, `models/__init__.py`, `services/banking/subscriptions.py` (stockage), `services/banking/transfer_patterns.py` (empreinte), `services/account_data.py`, `services/bank.py` (`delete_bank_account`).

- [x] Table `bank_subscriptions` : `uuid`, `user_uuid_bidx` (index), `status_enc` (`confirmed` | `refused`), `anchors_enc` (JSON des `hash_index(uuid)` des occurrences à la décision), `includes_enc`, `excludes_enc` (JSON, nullables), `identity_enc` (JSON : mots marchand, uuid des comptes, cadence, montant, moyen de paiement), `name_enc`, `cadence_enc`, `ended_on_enc` (nullables : nom, cadence forcée, résiliation déclarée), `created_at`, `updated_at` (posés par le service, à la microseconde).
- [x] `source_digest` inclut `(count, max(updated_at))` de la table ; `_VERSION` → `"10"`.
- [x] Export de compte : décisions déchiffrées (statut, nom, cadence, mots, comptes, dates ; pas les ancres). Purge : `wipe(BankSubscription, …)`.
- [x] Suppression d'un compte bancaire : décisions dont tous les comptes d'identité sont ce compte supprimées ; ce compte retiré des autres.
- [x] Tests : upgrade / downgrade / upgrade ; purge (mutation : retirer le `wipe` rougit) ; export ; suppression de compte (mutation) ; **empreinte** : écrire une décision fait tomber le cache des patterns sans qu'aucune opération ne change.

## Tâche S4 — Détection dans la reconstruction des patterns

**Fichiers :** `services/banking/flows.py` (`transfer_patterns`), `services/banking/transfer_patterns.py`, `services/banking/subscriptions.py`.

- [x] Dans la reconstruction, après `resolutions` et `_flow_groups` : entrées (§1) depuis `movements`, `labels`, `transfer_legs`, `resolutions` ; marchands (S1) sur débits et crédits non appariés ; `recurrence` (S2) ; rattachement des décisions et corrections (§9) ; confiance ; porteur ; question (supprimée si le groupe de flux du porteur a sa question ouverte, ou si la série est décidée) ; membres **comptés** (série `certain` non refusée, ou confirmée : occurrences et extras typés Dépense, remboursements liés typés Dépense).
- [x] `TransferPatterns` gagne `subscriptions` (par série : clé, décision, confiance, cadence, variable, membres `[uuid, rôle]` avec rôle `regular | extra | cancelled | refund | manual`, niveaux, épisodes, dernière occurrence, montant actuel, nom affiché — `label_groups.group_name` des occurrences, la plus récente à égalité —, noms anciens, comptes, moyen de paiement, porteur) et `subscription_questions` (`"YYYY-MM"` du porteur → nombre). L'index opération → série se reconstruit à la lecture. Lecture / écriture ; `_VERSION` déjà incrémentée (S3).
- [x] Performance : reconstruction chronométrée sur le dump avant / après (référence ~320 ms) ; si > +200 ms : passe B sautée pour un marchand à plus de 2,5 débits par mois sur son étendue (il ne peut atteindre 0,75 d'exclusivité), passe A par grappes de montants.
- [x] Tests (mutation, base réelle de test) : un prélèvement mensuel de 8 échéances devient compté sans question ; une série carte de 3 échéances porte sa question, comptée dans `patterns.subscription_questions` ; loyer par virement avec question de flux ouverte → pas de question d'abonnement, puis réponse « Dépense » → la question apparaît ; loyer typé Neutre par règle → rien ; jambe de virement interne jamais membre ; reconstruction déterministe (deux passes, même contenu).

## Tâche S5 — Décisions et corrections

**Fichiers :** `services/banking/subscriptions.py`, `routes/banking.py`, `dtos/banking.py`.

- [x] `POST /banking/subscriptions/decisions` — `{transaction_id, decision: "confirm" | "refuse", name?}` : décide de la série qui contient l'opération ; ancres = ses occurrences ; remplace une décision déjà rattachée à cette série.
- [x] `POST /banking/subscriptions` — `{transaction_id, cadence?, name?}` : marquer une opération (graine, §9). 409 si l'opération n'est pas un débit final typé Dépense hors paire.
- [x] `PATCH /banking/subscriptions/{id}` — `{name?, cadence?, ended_on?}`.
- [x] `POST /banking/subscriptions/{id}/operations` — `{transaction_id, action: "include" | "exclude"}`.
- [x] `POST /banking/subscriptions/{id}/merge` — `{other_id}` : ancres, inclusions et exclusions réunies, l'autre supprimé.
- [x] `DELETE /banking/subscriptions/{id}` : la série redevient candidate.
- [x] Rattachement (§9) dans la reconstruction : décisions par `updated_at` croissant ; ancres d'abord, identité ensuite ; graines ; inclusions puis exclusions.
- [x] Tests de routes (mutation) : 404 opération d'un autre utilisateur ; 404 abonnement inconnu ; confirmer puis importer une nouvelle échéance → elle est comptée sans nouvelle question ; refuser → plus de question, rien compté, retenu après une nouvelle échéance ; réimport sous d'autres uuid → décision retrouvée par identité ; marquer un annuel vu une fois → abonnement d'une occurrence, prochaine échéance à un an ; inclure un débit renommé non détecté → membre ; exclure une occurrence → plus comptée ; fusion ; suppression → candidate à nouveau.

## Tâche S6 — Opérations, questions, file, grand livre

**Fichiers :** `services/banking/flows.py` (`_item_builder`, `list_month_transactions`, `review_queue`), `services/banking/ledger.py`, `routes/banking.py` (`/transfer-questions`), `dtos/banking.py`.

- [x] `BankTransactionItem` gagne `subscription: {id, name, cadence, role, state: "auto" | "confirmed"} | None` et `subscription_question: {cadence, amount, variable, occurrence_count, since, annual_estimate, renamed_from: [str]} | None` (remplace `is_subscription` / `subscription_id` prévus au plan du 2026-09-16).
- [x] `transfer_questions` du mois et `GET /banking/transfer-questions` comptent `subscription_questions`.
- [x] `review_queue` : `BankReviewKind.SUBSCRIPTION`, montant = coût annuel estimé ; `subscription_count` ; pas dans `total_amount` (décision 5) ; `years` les compte.
- [x] Crédit lié à un abonnement compté (décision 4) : son groupe de flux pose sa question **même sous** `FLOW_QUESTION_MIN_AMOUNT` — ajouté aux `flow_carriers` après la détection (il ne touche pas aux questions de débit qui la précèdent) et reconnu par `list_flow_group` ; `flow_question.suggested = EXPENSE` et `subscription_name`.
- [x] Grand livre : `BankLedger.subscriptions: [{id, name, cadence}]`, `BankLedgerRow.subscription` (index, lignes comptées seulement), `question: "subscription"` ; `LEDGER_VERSION` → `"4"`.
- [x] Tests (mutation) : une ligne membre porte son abonnement et son rôle, une annulée porte `cancelled` sans compter ; la question est sur le porteur seul ; `transfer-questions` et le badge du mois la comptent ; file triée par montant avec l'abonnement à son coût annuel, hors `total_amount` ; suggestion « Remboursement » sur le crédit EDF lié ; **parité** : somme des `signed` des lignes comptées portant un abonnement = `subscriptions` du Réel, mois par mois.

## Tâche S7 — Liste des abonnements

**Fichiers :** `services/banking/subscriptions.py`, `routes/banking.py`, `dtos/banking.py`.

- [x] `GET /banking/subscriptions` → `{currency, monthly_total, annual_total, items}` ; totaux = abonnements **actifs** comptés, en devise principale (les autres à part). Par abonnement : `id` (décision, ou `null` pour un `auto` jamais décidé), `transaction_id` (dernière occurrence, pour agir), `name`, `state` (`auto` | `confirmed` | `candidate` | `refused`), `confidence`, `status` (`active` | `late` | `ended` | `stale` + `covered_until`), `cadence`, `variable`, `amount`, `currency`, `monthly_equivalent`, `annual_estimate`, `paid_last_12_months`, `first_date` (+ `since_at_least` quand le premier débit est à moins d'une échéance du début de couverture du compte), `last_date`, `next_date`, `occurrence_count`, `extra_count`, `accounts`, `payment_method`, `price_changes: [{date, before, after, percent}]`, `episodes: [{start, end}]`, `renamed: [{date, before, after}]`, `refunds: {total, items: [{date, amount, label}]}`, `ended_on`.
- [x] Ordre : actifs par équivalent mensuel décroissant, en retard, candidats, terminés (le plus récent d'abord), refusés.
- [x] `GET /banking/subscriptions/{id}/operations` (ou `?transaction_id=` pour une série sans décision) → `BankTransactionItem` des membres, les plus récents d'abord (comme `flow-group`, une passe sur l'historique).
- [x] Temps noté sur le dump (lecture des patterns seule).
- [x] Tests (mutation) : statuts actif / en retard / terminé / à jour au (compte non synchronisé) au jour près ; `since_at_least` ; coût annuel fixe (4 semaines → ×13) et variable ; changements de prix entre niveaux seulement (Fitness Park 35 → 10 → 30 : aucun) ; renommage listé ; remboursements liés ; `monthly_total` ignore terminés, refusés, candidats.

## Tâche S8 — Réel

**Fichiers :** `services/banking/real_cashflow.py`, `dtos/banking.py`.

- [x] `RealCashflowTotals.subscriptions` : dépenses des membres comptés, signées comme les dépenses (un remboursement typé Dépense en moins), **incluses** dans `expenses`, hors formule de `net` ; mois, année, moyenne, médiane, projection, année précédente.
- [x] Détail d'un mois : `subscriptions: [{id, name, amount, count}]`.
- [x] Année en cours : `fixed_charges` = somme des équivalents mensuels des abonnements actifs.
- [x] Mois en cours (`/real-cashflow/current`) : `upcoming: [{id, name, date, amount}]` et `upcoming_amount` — échéances attendues d'ici la fin du mois pour les abonnements actifs, sans celles déjà passées (finales ou en attente correspondant au montant et à l'échéance) ni celles d'un compte `stale`.
- [x] Tests (mutation) : `subscriptions` ≤ `expenses` ; paire jamais comptée ; occurrence annulée exclue ; remboursement typé Dépense déduit ; détail du mois ; `fixed_charges` ; `upcoming` sans l'échéance déjà en attente.

## Tâche S9 — Rejeu réel et contrôles

- [x] Sur le dump, **par l'API** (pas le prototype) : les 15 abonnements du tableau, mêmes confiances ; aucun des « jamais proposés » ; écart expliqué sinon.
- [x] `stability.py` rejoué contre le service (mêmes séries à chaque fin de mois).
- [x] Temps de reconstruction, `/banking/subscriptions`, `/banking/transactions`, `/banking/review-queue`, `/banking/ledger`, `/banking/real-cashflow` notés ici.
- [x] Base : aucune valeur en clair dans `bank_subscriptions` ; aucune colonne jointable avec `bank_transactions`.

# Partie W — Web (plus tard, esquisse)

- **Opérations** : badge « Abonnement » sur les membres (« hors échéance », « remboursé ») ; question sur le porteur au style des virements douteux : « Abonnement mensuel · 21,60 € depuis juillet ? » Oui / Non, avec « a changé de nom » quand il y a lieu ; comptée dans le badge, les puces et le filtre « à vérifier ».
- **À trier** : les questions d'abonnement avec leur coût annuel, hors « X € à confirmer ».
- **Onglet Abonnements** : total mensuel et annuel des actifs ; liste (montant, cadence, prochaine échéance, hausse signalée, « en retard », « à jour au ») ; détail : courbe du montant par niveau, occurrences, remboursements, épisodes, anciens noms ; actions : renommer, fusionner, rattacher / détacher, « ce n'est pas un abonnement », « je l'ai résilié » ; candidats, terminés, refusés repliés.
- **Réel** : « dont abonnements » sur la carte Dépenses, charges fixes, abonnements du mois ; mois en cours : « À venir ce mois : N prélèvements, X € ».
- **Explorer** : filtre et regroupement « Abonnement ».

## Vérification finale

1. `uv run pytest -q` vert, nombre de tests noté ; mutation faite sur chaque test neuf.
2. Migration upgrade / downgrade / upgrade en dev, puis une lecture qui force la reconstruction (`_VERSION` 10).
3. S9 : liste, stabilité, temps, base.
4. `git status --short` : ni `main.py` (montage du routeur de debug), ni `.claude/`, ni l'outillage de `.git/info/exclude` dans un commit.

## Après ce plan

1. **Web** (Partie W), détaillé dans son propre plan.
2. **Alertes** : hausse de prix, prélevé après une résiliation déclarée (`ended_on`), double prélèvement, première échéance pleine après un essai.
3. **Identité marchand partagée** : faire lire `merchants.py` par `label_groups` (Explorer, Réel) et par la portée des règles de type — `VIR INST FREDERIC DURAND` n'est pas atteint par la règle de `VIR SEPA Frederic Durand` (Jaccard 0,5 à cause de `inst` / `sepa`). À mesurer, `LEDGER_VERSION` à incrémenter.
4. **Revenus récurrents** (salaire, CAF, aides) : le moteur est indépendant du sens.
5. **Visé ↔ réel** : rattacher une ligne du visé (« Loyer », « Forfait mobile ») à un abonnement.

## Rapport d'implémentation (2026-09-18)

Branche `feat/subscriptions`, API seule. Décisions 1, 2, 4, 5 retenues comme recommandé ; décision 3 : la règle Neutre du loyer Frederic Durand est une réponse saisie dans l'app, rien dans le code n'en dépend.

**Modules** : `merchants.py` (S1), `recurrence.py` (S2), `subscription_decisions.py` (stockage S3, à part pour que `services/bank.py` l'importe sans cycle), `subscription_series.py` (S4-S5 : dérivation dans la reconstruction, sans lecture ni import de `flows`), `subscriptions.py` (S5-S7 : service des routes). Migration `8d4e2a6b1c93`.

**Écarts au plan**, chacun couvert par un test et une mutation :

- **Troncature** (S1) : le dernier mot d'un libellé, hors premier, peut être le début du mot au même rang de l'autre (`EA *ELECTRONIC AR`) ; hors dump, le seul préfixe ≥ 4 lettres ne les réunissait pas.
- **Niveaux de prix** (S2) : une série « flotte » (±3 %) quand moins de la moitié de ses pas tombent au centime, et non « moins de 60 % à ±1 % » : ce seuil basculait au hasard sur une gigue de change de ±1,5 % (5 tirages sur 10 coupés en 2 à 4 niveaux). EDF et MACIF gardent leurs niveaux du plan.
- **Virement** (S2) : exclusivité ≥ 0,5 exigée. Des virements aléatoires à un ami formaient une chaîne `probable` (exclusivité 0,25) sur 2 graines sur 30 ; le loyer Transalp'Dome est à 0,75.
- **Cadence imposée** (S5) : une décision dont l'utilisateur a fixé la cadence ne se rattache par ses ancres qu'à une série de cadence compatible ; sinon elle part de sa graine (un débit annuel marqué n'est pas la série mensuelle d'achats où il est tombé).
- **Remboursements liés** (§9) : même marchand **ou** même premier mot (comme le prototype) : EDF rembourse sous `EDF CLT PART RBT`. Les 6 crédits de la décision 4 sont liés.
- `BankSubscriptionItem`, `BankLedgerSubscription`, `RealCashflowSubscription` et `RealCashflowUpcoming` portent aussi `key` (id de la décision, sinon de la première occurrence) : un abonnement jamais décidé a `id: null`.
- Fusion : `{other_id}` ou `{other_transaction_id}` (l'autre peut n'avoir jamais été décidé).

**S9 sur le dump (service de l'API, utilisateur de debug)** : les 15 abonnements du tableau, mêmes confiances (6 sûrs, 9 probables), aucun « jamais proposé » ; 7 actifs ; 6 remboursements liés. Stabilité rejouée à chaque fin de mois 2025-03 → 2026-08 : aucun trou, aucune confiance qui redescend, EDF de 17 à 29 occurrences, MACIF de 6 à 22.

| Mesure | `main` | branche |
|---|---|---|
| reconstruction des patterns | 373 ms | 510-560 ms (dont abonnements ≈ 162 ms) |
| `/banking/transactions` (un mois) | 19 ms | 23 ms |
| `/banking/review-queue` | 148 ms | 165 ms |
| `/banking/ledger` | 242 ms | 281 ms |
| `/banking/real-cashflow` | 109 ms | 123 ms |
| `/banking/real-cashflow/current` | 69 ms | 84 ms |
| `/banking/subscriptions` | — | 5 ms |

La dérivation dépassait le budget (220 ms) avant l'indexation des renommages par compte, moyen et montant ; passe B non élaguée.

**Vérifications** : `uv run pytest -q` → 1715 tests (1569 avant) ; mutation sur chaque test neuf, aucun survivant ; migration upgrade / downgrade / upgrade en dev. `bank_subscriptions` est vide en dev : l'absence de valeur en clair est vérifiée par les tests (`test_nothing_about_…`), pas sur la base.

**Reste à faire** : la partie Web ; les noms affichés reprennent `label_groups.group_name` et sont parfois longs sous le nouveau format Boursorama (`TRANSALP'DOME S.A.S. Virement pour le loyer…`) — le renommage est possible, un nettoyage des libellés longs est à faire avec l'identité marchand partagée (« Après ce plan », point 3).

## Renommage du 2026-09-20 : abonnement → paiement récurrent

La fonctionnalité ne détecte pas des abonnements mais **toute charge qui revient** —
loyer, crédit, énergie compris. Le vocabulaire a suivi, avant tout push et sur une
table encore vide ; ce plan garde le mot « abonnement » là où il raconte ce qui a
été décidé le 2026-09-18. Correspondance :

| Avant | Après |
|---|---|
| `services/banking/subscriptions.py` | `services/banking/recurring.py` |
| `services/banking/subscription_series.py` | `services/banking/recurring_series.py` |
| `services/banking/subscription_decisions.py` | `services/banking/recurring_decisions.py` |
| table `bank_subscriptions`, migration `8d4e2a6b1c93` | table `bank_recurring_series`, migration `fb7c7e2f233b` |
| `BankSubscription`, `StoredSubscription`, `BankSubscription*` | `BankRecurringSeries`, `StoredRecurring`, `BankRecurring*` |
| `GET /banking/subscriptions` | `GET /banking/recurring` |
| `RealCashflowTotals.subscriptions` | `RealCashflowTotals.recurring` |
| onglet « Abonnements » | onglet « Récurrent » |

Le moteur (`recurrence.py`) ne change pas de nom : il portait déjà le bon.
`_VERSION` des patterns passe à `11`, deux clés du JSON stocké ayant changé.
Reste une collision de vocabulaire à connaître : `BankTransferStatus.RECURRING`
désigne une **paire** de virements vue souvent, sans rapport avec un paiement
récurrent.

## Nature du 2026-09-20 : à quoi sert le paiement

Chaque paiement récurrent porte une **nature** sur une liste fermée — Logement,
Énergie, Assurance, Crédit, Télécom, Transport, Sport, Loisirs, Logiciels, Autre.
Elle est **devinée du marchand** (`services/banking/natures.py`, un mot → une
nature, rien de stocké : enrichir le dictionnaire améliore l'existant sans
reconstruction) et l'utilisateur la corrige d'un menu, ce qui la stocke dans sa
décision (`nature_enc`). `nature_set` dit laquelle des deux l'écran affiche.

Les quatre premières sont les **charges fixes** : ce qu'un mois ne peut pas
éviter. L'onglet les sépare des **abonnements**, résiliables, avec un total par
bloc ; c'est là que le loyer cesse de noyer le reste. La frontière est tenue
côté web (`utils/recurring.ts`), seul endroit qui en a besoin aujourd'hui.

**Reste à faire** : « dont charges fixes » / « dont abonnements » dans le Réel,
qui demande que `RealCashflowTotals` porte la coupure côté API ; et l'historique
par nature (le loyer d'un bail à l'autre).

## Hors périmètre

Catégories de dépenses, IA, lecture des dates dans les libellés, base de marchands externe (logos, noms officiels), apprentissage entre utilisateurs.
