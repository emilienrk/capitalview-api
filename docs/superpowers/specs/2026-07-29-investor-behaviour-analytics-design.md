# Analyse comportementale de l'investisseur — design

Date : 2026-07-29
Statut : **proposition, en attente de validation**
Périmètre : comptes bourse (PEA, PEA-PME, CTO). Crypto hors périmètre.

---

## 0. Cadrage validé

| Décision | Choix |
|---|---|
| Périmètre | Bourse / ETF uniquement. Le moteur reste agnostique pour brancher la crypto plus tard. |
| Benchmark | ETF MSCI World UCITS **capitalisant** (EUR), configurable en réglages. Le caractère capitalisant est une contrainte, pas une préférence — cf. §7.1. |
| Plan cible | **Optionnel.** Tout fonctionne sans. Un bloc supplémentaire s'active si le plan est rempli. |

La question à laquelle la page doit répondre :

> « Quelle est réellement ma stratégie — pas celle que je crois avoir — est-ce que je l'exécute
> correctement, et où est-ce que mon comportement me coûte de l'argent ? »

---

## 0 bis. Principe structurant : achat ≠ dépôt

**La discipline se mesure sur les achats. Jamais sur les dépôts.**

Déposer de l'argent et l'investir sont deux comportements distincts, et les confondre produit un
verdict faux. Trois profils donnent la même courbe de dépôts et des stratégies opposées :

- dépôts erratiques, achat immédiat ⇒ **la discipline est celle des dépôts**, l'achat ne fait que
  suivre ;
- dépôts mensuels réguliers, achats groupés au gré des opportunités ⇒ **régularité apparente,
  market timing réel** ;
- dépôts erratiques, achats systématiques en fin de mois ⇒ **investisseur discipliné** qu'une lecture
  par les dépôts qualifierait à tort de chaotique.

Règle d'attribution appliquée partout dans ce design :

| Source | Ce qu'elle sert |
|---|---|
| **Achats (`BUY`)** | Régularité, discipline, conditionnement au marché, exécution, baseline contrefactuelle. **Tout ce qui juge le comportement d'investissement.** |
| **Dépôts / retraits externes réels** | MWR/XIRR (par définition : l'argent qui entre et sort de la poche), cash drag, et le décalage dépôt→investissement (§2.4). Rien d'autre. |
| **Dépôts auto-provisionnés** | **Exclus de toute statistique.** Ce sont des écritures synthétiques créées par l'app 1 s avant un `BUY` (`services/stock_transaction.py:308-327`, note `"Provision automatique"`), pas des décisions. |

Conséquence sur le calcul du MWR : les provisions automatiques ne sont pas des flux externes au sens
économique — elles sont datées sur l'achat, pas sur le virement réel. Elles sont donc retirées de la
série de flux, et le MWR est calculé sur les seuls dépôts et retraits authentiques. Quand la part de
provisions automatiques est élevée, l'UI l'affiche : cela signifie que la date réelle d'entrée de
l'argent n'est pas connue, et le MWR devient `indicatif`.

---

## 1. Où ça vit

**Une page dédiée `/analyse`**, entrée de nav propre, plus un point d'entrée discret depuis `/stock`.

### Pourquoi pas dans `/stock`

`capitalview-web/src/pages/Stock.vue` fait déjà 2049 lignes et c'est une page **transactionnelle** :
comptes, positions, transactions, imports, modales. Son mode mental est « gérer ». L'analyse
comportementale a le mode mental inverse : « se confronter ». Mélanger les deux garantit que
l'analyse est survolée entre deux saisies.

### Pourquoi je ne refonds pas les 8 encarts

Les 8 encarts actuels (`stockSummaryStats`, `Stock.vue:377-473`) sont **scopés compte** et
**descriptifs d'état** : Investi, Valeur, P/L, Perf, Dividendes, Liquidités, Dépôts, Retraits, avec
un jeu de cartes déjà adapté au type de compte (PEA vs CTO) et une bascule latent/réalisé/total.
Ils répondent à « où j'en suis sur ce compte ». C'est le bon contenu pour ce job.

Les métriques comportementales sont **scopées portefeuille** et **temporelles** : elles n'ont pas de
sens dans une grille de 4 cartes par compte, et les mettre là casserait le flux de saisie. Je n'y
touche pas.

### Pourquoi pas le Dashboard

Le Dashboard est une vue patrimoine toutes classes d'actifs (banque + bourse + crypto + biens).
Mauvaise altitude : l'analyse comportementale est une plongée sur une seule classe d'actifs.

### Découpage de la page

```
/analyse
├── Verdict            — 3 à 5 phrases générées, brutales, avec les chiffres dedans
├── Bloc 1 · Ce que je fais vraiment          (régularité des achats, décalage dépôt→achat,
│                                              conditionnement marché)
├── Bloc 2 · Ce que ça me coûte               (écart investisseur, pont contrefactuel, exécution)
├── Bloc 3 · Ce que je détiens vraiment       (paris indépendants, concentration)
├── Bloc 4 · Frais
├── Bloc 5 · Adhérence au plan                (masqué si aucun plan déclaré)
└── Notes de méthode                          (repliable : conventions, limites, ce que je ne calcule pas)
```

---

## 2. Le principe directeur : le cadre de fiabilité

C'est la partie la plus importante du design, avant toute métrique.

**Deux ans de données, c'est peu.** Le piège classique n'est pas de mal calculer, c'est d'afficher
un chiffre juste avec une confiance fausse. Chaque métrique porte donc un statut :

| Statut | Rendu UI | Règle |
|---|---|---|
| `solide` | valeur en gros, lecture affirmative | échantillon suffisant, méthode non paramétrique ou robuste |
| `indicatif` | valeur en gris, encart « pourquoi c'est fragile » inline | calculable mais sous le seuil de conclusion |
| `insuffisant` | **la valeur n'est pas affichée** — on affiche ce qui manque et pourquoi | sous le seuil minimal |

Gates concrètes :

- Aucune annualisation présentée comme une vérité sous 3 ans. On montre le **cumulé** en gros et
  l'annualisé en petit, avec la mention explicite.
- Effet de disposition : `insuffisant` sous 12 occasions de réalisation.
- Tout écart comportemental (timing, exécution) passe par un **test de permutation** avant d'être
  formulé affirmativement. `p > 0.10` ⇒ la lecture devient « rien de détectable », pas « tu es bon ».
- Métriques de corrélation : minimum 250 rendements journaliers chevauchants par paire.
- Comparaison année 1 vs année 2 : toujours étiquetée « tendance, pas preuve » (12 mois par bucket).

Structure de retour de chaque métrique :
`{ value, unit, sample_size, reliability, caveat, verdict_text }`.

---

## 3. Les métriques, classées par impact

### TIER 1 — les trois qui peuvent changer un comportement

---

#### 1.1 · L'écart investisseur : MWR − TWR

**Ce que c'est.** Deux mesures de « ma performance » qui ne mesurent pas la même chose :

- **TWR** (time-weighted return, chaînage journalier neutralisé des flux) = performance de la
  **stratégie**. C'est ce que la norme GIPS impose aux gérants, précisément parce qu'elle est
  insensible au moment où l'argent entre.
- **MWR** (money-weighted / dollar-weighted, XIRR sur les flux réels) = performance de
  **l'investisseur**. Elle intègre le fait que 10 000 € mal placés dans le temps pèsent plus que
  1 000 € bien placés.

**L'écart MWR − TWR est la mesure standard du comportement.** C'est l'objet de Dichev (2007,
*American Economic Review*) sur les rendements dollar-weighted, et de l'étude annuelle *Mind the
Gap* de Morningstar. Ordre de grandeur documenté sur les fonds : environ **1 point par an au
détriment de l'investisseur** — donné ici comme repère, pas comme prédiction.

**Calcul.**
- TWR : à partir des snapshots journaliers `AccountHistory.total_value` déjà en base, et du flux
  externe net quotidien `F_d` (DEPOSIT − WITHDRAW en EUR). `r_d = (V_d − V_{d−1} − F_d) / (V_{d−1} + F_d)`,
  convention **flux en début de journée** (Modified Dietz journalier), puis chaînage `∏(1+r_d) − 1`.
  Jours où `V_{d−1} + F_d ≤ 0` : reportés, comptabilisés et signalés.
- MWR : XIRR sur (flux externes signés + valeur terminale), résolu par **bissection** sur `[−0,99 ; 10]`
  (converge toujours en présence d'un changement de signe, contrairement à Newton). Les dépôts
  auto-provisionnés sont exclus de la série de flux (§0 bis) ; leur part est reportée et fait
  basculer le MWR en `indicatif` au-delà de 30 %.

**Le « et donc ? ».** L'écart est traduit **en euros** (`écart annualisé × capital moyen investi`),
pas seulement en points. C'est le chiffre qui marque.

> « Ta stratégie a fait +14,2 %. Toi, tu as fait +11,8 %. L'écart de 2,4 points, sur ton capital
> moyen, représente **−1 340 €**. Il ne vient pas de tes choix d'actifs : il vient du moment où tu
> mets l'argent. »

**Limites affichées.** Sur 2 ans, le signe de l'écart est lisible, sa magnitude annualisée l'est
beaucoup moins. Statut `indicatif` tant qu'on est sous 3 ans.

---

#### 1.2 · Le pont contrefactuel — « combien mon comportement m'a coûté vs un robot »

**Ce que c'est.** Un waterfall qui part d'une baseline mécanique et arrive à ton portefeuille réel,
en remplaçant **une décision à la fois**.

Baseline « le robot » : le même capital total **effectivement investi**, réparti en **achats
mensuels égaux** entre ton premier et ton dernier achat, sur le benchmark, zéro frais, zéro cash
dormant.

```
Robot (achats mensuels égaux sur MSCI World)            ............  X €
  + ton calendrier réel d'ACHATS        → effet timing d'investissement ± a
  + tes actifs réels (buy & hold)       → effet sélection               ± b
  + tes prix d'exécution réels          → effet exécution intra-mois    ± c
  + ton cash réellement resté dormant   → cash drag                     ± d
  + tes frais réels                                                     − e
  + tes ventes et arbitrages            → effet des sorties             ± f
= Ton portefeuille réel                                 ............  Y €
```

Le terme **a** porte sur les dates et montants d'**achat**, conformément au §0 bis — c'est la
décision d'investissement, pas le virement. Les dépôts n'entrent que par le terme **d** : le cash
drag mesure le coût du délai entre l'arrivée réelle de l'argent et son investissement. Si tu
investis systématiquement à réception, ce terme est nul — et c'est un résultat, pas un angle mort.

**D'où ça vient.** La logique de décomposition additive d'un écart de performance en effets
attribuables est celle de l'attribution Brinson (Brinson–Hood–Beebower 1986, Brinson–Singer–Beebower
1991). **Je l'assume comme adaptation, pas comme du Brinson standard** : le Brinson canonique
décompose en allocation / sélection / interaction sur des segments sectoriels, ce qui exigerait un
look-through des ETF qu'on n'a pas. Ici la segmentation porte sur les **décisions**, pas sur les
secteurs. C'est du sur-mesure, et l'UI le dira.

**Garantie de correction.** La somme doit réconcilier **exactement** avec la valeur réelle du
portefeuille. C'est testé. Si un résidu subsiste, il apparaît comme une barre « non expliqué »
explicite — jamais absorbé silencieusement dans un autre terme.

**Limite affichée.** La décomposition est **dépendante du chemin** : l'ordre des substitutions est un
choix, et le réordonner déplace quelques points de pourcentage entre termes adjacents. L'ordre retenu
est affiché, et la note de méthode l'explique.

**Le « et donc ? ».**

> « Un robot bête, qui aurait acheté 480 €/mois de MSCI World sans jamais réfléchir, aurait
> 24 180 €. Tu en as 23 050 €. **Ton intelligence t'a coûté 1 130 €** — dont 620 € de sélection
> d'actifs et 340 € de cash resté sur le compte. »

---

#### 1.3 · Coût d'exécution vs prix moyen de la période

**Ce que c'est.** De la Transaction Cost Analysis appliquée au particulier. Pour chaque achat, on
compare le prix payé au **prix moyen de l'actif sur le mois calendaire de l'opération**.

**Nommage honnête.** L'industrie parle de *interval VWAP*. On n'a que des cours de clôture
journaliers en base — donc c'est un **TWAP sur clôtures journalières**, pas un VWAP. L'UI l'écrira
comme ça. La variante « ±10 jours de bourse centrés » est proposée en second axe.

Le vrai *implementation shortfall* (Perold 1988) exige un horodatage de décision qu'on n'a pas.
**Je ne le calcule pas et je ne prétends pas le calculer.**

**Calcul.** Slippage par ordre en points de base, agrégé pondéré par le montant, plus la
**distribution complète** (box plot) — parce que la moyenne cache si c'est un biais systématique ou
deux ordres catastrophiques.

**Test de permutation.** Chaque achat est re-daté aléatoirement parmi les jours de bourse de **son
propre mois**, à montant et actif constants. 5 000 tirages. On regarde où tombe le slippage réel.
Ce test annule exactement « le choix du jour dans le mois » en gelant tout le reste : c'est la seule
façon honnête de distinguer un biais d'exécution de 47 coups de dés.

**Le « et donc ? ».**

> « Sur 47 achats, tu paies en moyenne **+18 bps** au-dessus du prix moyen du mois, soit 142 € sur
> 2 ans. Le test de permutation le classe au 96ᵉ centile : ce n'est pas du bruit, **tu achètes
> systématiquement après la hausse du mois.** »

ou, si c'est neutre :

> « Slippage moyen −2 bps, p = 0,61. Ton timing d'exécution ne te coûte rien et ne te rapporte rien.
> **Ce n'est pas là qu'il faut chercher.** »

---

### TIER 2 — les vérités structurelles

---

#### 2.1 · Régularité réelle d'investissement — sur les achats

**Ce que c'est.** La réponse directe à « quelle est réellement ma stratégie ». Calculée sur les
**montants achetés par mois** (§0 bis), jamais sur les dépôts :

- part des mois avec au moins un achat ;
- coefficient de variation des montants investis mensuels ;
- plus longue interruption d'investissement ;
- **HHI temporel** (indice de Herfindahl–Hirschman appliqué à la répartition du capital investi dans
  le temps) et son inverse : le **nombre d'achats mensuels égaux équivalents** ;
- **régularité du jour du mois** : dispersion du jour de l'achat principal, qui distingue un « je
  passe mes ordres le 5 » d'un « j'achète quand j'y pense ».

L'HHI est un indice standard de concentration ; son application à l'axe temporel des achats est mon
usage, et sera présentée comme telle.

**Forme.** Heatmap calendaire des montants investis par mois + les 5 chiffres.

**Le « et donc ? ».**

> « Tu penses faire du DCA. Sur 24 mois tu as investi 14 fois, et **52 % de ton capital est parti en
> 3 mois**. Ton HHI temporel de 0,21 équivaut à **4,8 achats mensuels égaux, pas 24.** Tu ne fais pas
> du DCA, tu fais des achats opportunistes que tu appelles DCA. »

---

#### 2.4 · Le décalage dépôt → investissement

**Ce que c'est.** La métrique que la séparation du §0 bis fait apparaître, et qui répond directement
à « est-ce que mon rythme de dépôt et mon rythme d'achat sont le même comportement ? ».

Pour chaque dépôt externe réel (provisions automatiques exclues), on suit l'argent : combien de jours
s'écoulent avant qu'il soit investi, en appariant les dépôts aux achats en **FIFO** sur le solde de
liquidités.

Trois sorties :

1. **Distribution du délai** dépôt → investissement (médiane, quartiles, queue).
2. **Comparaison des deux rythmes** : régularité des dépôts vs régularité des achats, côte à côte,
   avec les mêmes indicateurs qu'en §2.1.
3. **Coût du délai** : le cash drag du §1.2 terme *d*, rattaché ici à sa cause.

**Le « et donc ? ».** C'est la métrique qui identifie le profil, et chaque profil appelle une action
différente :

> « Tes dépôts sont réguliers (CV 0,18), tes achats ne le sont pas (CV 0,71). L'argent dort en
> médiane **23 jours** avant d'être investi, et ce délai t'a coûté **190 €**. **Ta discipline
> s'arrête au virement.** »

ou :

> « Tes dépôts sont erratiques mais tu investis dans les 24 h dans 91 % des cas. **Ton irrégularité
> apparente est celle de ton épargne, pas de ta stratégie** — ne cherche pas à corriger le mauvais
> comportement. »

---

#### 2.2 · Conditionnement au marché : contrarian ou suiveur ?

**Ce que c'est.** Pour chaque euro investi, on relève l'état du benchmark ce jour-là : (a) drawdown
depuis son plus haut glissant 1 an, (b) rendement des 21 derniers jours de bourse. On compare la
**distribution pondérée par les euros de mes achats** à la **distribution inconditionnelle de tous
les jours de bourse** de la même fenêtre.

**D'où ça vient.** C'est la mesure du *return chasing* / des anticipations extrapolatives, documenté
par Greenwood & Shleifer (2014) et, côté flux, par Frazzini & Lamont (2008, « Dumb money »).

**Forme.** Deux densités superposées, plus un nuage de points (date × drawdown, taille = €).

**Test de permutation.** Les dates d'achat sont permutées sur l'ensemble des jours de bourse de la
fenêtre, montants constants, 5 000 tirages. Hypothèse nulle : « mon argent entre un jour au hasard ».

**Découpage année 1 / année 2** pour répondre à « est-ce que j'évolue ? », avec l'avertissement
« 12 mois par bucket » en évidence.

**Le « et donc ? ».**

> « Ton euro moyen entre quand le marché est à **−3,1 %** de son plus haut. Un jour moyen, c'est
> **−5,4 %**. Tu achètes plus haut que le hasard (p = 0,03). **Tu attends la confirmation, et la
> confirmation se paie.** »

---

#### 2.3 · Diversification réelle : le nombre de paris indépendants

**Ce que c'est.** Trois chiffres qui divergent, et c'est tout l'intérêt :

1. le nombre de lignes détenues ;
2. le **nombre effectif de positions** = `1/HHI` sur les poids (corrige le fait qu'une ligne à 2 %
   ne compte pas comme une ligne à 40 %) ;
3. le **nombre effectif de paris indépendants** : ACP sur la matrice de covariance des rendements
   journaliers, exposition du portefeuille aux composantes principales, puis entropie des
   contributions à la variance — `N_ent = exp(−Σ pᵢ ln pᵢ)`.

Le point 3 est la mesure de Meucci (2009, *Managing Diversification*, Risk), complétée
conceptuellement par le ratio de diversification de Choueifaty & Coignard (2008). Toutes les données
nécessaires sont **déjà en base** (`market_price_history`).

**Ce que ce n'est PAS, et l'UI le dira.** Ce n'est **pas du look-through**. On ne connaît pas la
composition des ETF, donc on ne peut pas dire « tu as 6 % d'Apple en double ». On mesure la
**redondance de comportement** : à quel point tes lignes bougent ensemble. C'est une mesure
différente, et elle est suffisante pour le verdict.

**Le « et donc ? ».**

> « Tu détiens 6 lignes. Pondérées, ça fait **3,4 positions effectives**. Statistiquement, ça fait
> **1,3 pari indépendant** : tes trois principaux ETF corrèlent à 0,94. **Ta diversification est une
> illusion de comptage.** Ajouter un 7ᵉ ETF World ne changera rien ; seul un actif décorrélé le
> ferait. »

**Limites.** 2 ans de rendements journaliers ⇒ estimation bruitée, et l'ACP sur peu d'actifs est
sensible. Statut `indicatif`, taille d'échantillon affichée.

---

### TIER 3 — peu cher, actionnable, honnête

---

#### 3.1 · Frais : le seuil de rentabilité de ton ticket moyen

Frais totaux, en % du capital déployé, en bps annualisés, distribution des frais par ordre rapportée
à la taille d'ordre.

Le chiffre qui change un comportement n'est pas « tu as payé 210 € de frais », c'est **le seuil** :

> « Ton courtier te prend en moyenne 4,20 € par ordre. En dessous de **380 € par ordre**, tu dépasses
> 25 bps de frais d'entrée. **12 de tes 47 ordres sont sous ce seuil** — ils t'ont coûté 34 € pour
> 2 900 € investis. Regroupe-les. »

Projection 20 ans à ton rythme actuel, **avec l'hypothèse écrite** (même cadence de versement,
5 %/an).

**Note d'honnêteté obligatoire dans l'UI** : les frais de courtage ne sont pas ton coût principal.
Le **TER des ETF** (typiquement 0,15–0,25 %/an) est déjà dans le cours et **n'est pas traçable ici**.
Sur un portefeuille buy-and-hold, il pèse structurellement plus lourd que les frais d'ordre.

---

#### 3.2 · Effet de disposition (PGR / PLR) — sous conditions

**Ce que c'est.** La mesure canonique d'Odean (1998, *Journal of Finance*) : à chaque jour de vente,
on compte les gains latents réalisés vs disponibles (PGR) et les pertes latentes réalisées vs
disponibles (PLR). `PGR/PLR > 1` ⇒ tu coupes tes gains et gardes tes pertes.

**Gate à 12 occasions de réalisation.** En dessous, on n'affiche pas de chiffre — et le message
d'insuffisance est lui-même un verdict :

> « Tu as vendu 3 fois en 2 ans. C'est trop peu pour mesurer quoi que ce soit. **C'est en soi
> l'information : tu es un accumulateur, pas un arbitragiste.** L'effet de disposition n'est pas ton
> problème — les métriques d'apport le sont. »

---

## 4. Bloc optionnel : adhérence au plan

Masqué tant qu'aucun plan n'est déclaré. Le plan comporte : **montant mensuel cible à investir**
(pas à déposer — §0 bis) + **allocation cible** (par ISIN ou par libellé, en %).

Une fois rempli, trois métriques :

- **Adhérence en €** : écart cumulé entre ce que le plan prévoyait et ce qui a été **investi**, mois
  par mois. En € et en % du plan.
- **Dérive d'allocation** : distance entre l'allocation réelle et l'allocation cible (norme L1, en
  points), et le montant à rééquilibrer.
- **Écart intention / exécution** : la comparaison la plus dure, parce qu'elle oppose ce que tu as
  écrit à ce que tu as fait.

> « Ton plan dit 500 €/mois investis. Tu as investi 8 400 € en 24 mois, soit **350 €/mois réels —
> 30 % sous ton propre plan.** Et les mois où tu as sous-investi sont à 71 % des mois de baisse du
> marché. »

Toutes les métriques des sections 3 restent calculées et affichées **sans** plan déclaré.

---

## 5. Ce que j'écarte explicitement

Une section repliable « ce que je ne calcule pas, et pourquoi » en pied de page, parce qu'un
indicateur absent doit être un choix visible, pas un oubli.

| Écarté | Pourquoi |
|---|---|
| Sharpe, Sortino, ratio d'information | Sur 2 ans, c'est du bruit. La t-stat d'un ratio d'information vaut ≈ IR·√T : il faudrait un IR > 1,4 pour être significatif sur 2 ans. Les afficher échouerait au test du « et donc ? ». |
| Alpha / bêta vs marché | Même problème d'échantillon, et redondant avec §2.3. |
| Max drawdown en chiffre-titre | Descriptif, non actionnable, et déjà lisible sur la courbe d'évolution existante. |
| Attribution Brinson–Fachler sectorielle | Exige les poids sectoriels du portefeuille **et** du benchmark. Sans look-through des ETF, ce serait de la fabrication. |
| Implementation shortfall (Perold 1988) | Exige un horodatage de décision qu'on ne collecte pas. |

---

## 6. Données manquantes — classées par valeur

| # | Donnée | Ce que ça débloque | Coût |
|---|---|---|---|
| 1 | **Plan cible** (mensuel + allocation) | Tout le §4. Transforme des descriptions en écarts par rapport à *ton* intention. | 1 colonne chiffrée + 1 formulaire. **Validé, optionnel.** |
| 2 | **Benchmark explicite** | Tout le Tier 1. | 1 colonne + 1 select. **Validé.** |
| 3 | **Composition des ETF (look-through)** | Vraie exposition géo/sectorielle/factorielle, vrai recouvrement entre lignes, vraie attribution Brinson. | Élevé : source externe (CSV émetteur, justETF…) ou taggage manuel. **Signalé, pas construit.** |
| 4 | **Journal de décision structuré** | Transforme des stats comportementales en récits causaux (« j'ai vendu parce que… »). Le champ `notes_enc` existe déjà par transaction — il suffirait de le structurer par tags. | Faible. **Signalé.** |
| 5 | **Horodatage de décision** | Vrai implementation shortfall. | Friction de saisie disproportionnée pour un particulier. **Signalé, déconseillé.** |

---

## 7. Constats de qualité de données trouvés pendant l'exploration

Trois choses que j'ai trouvées en explorant, qui affectent la rigueur de l'analyse. Aucune n'est
dans le périmètre de correction, mais elles doivent être dites.

### 7.1 · Le modèle « prix brut + dividendes saisis » est le bon — le backfill ne le respecte pas

Le choix de stocker des **prix bruts** dans `market_price_history` est délibéré et cohérent : la
table est mutualisée entre tous les utilisateurs, et chacun saisit ses propres dividendes en
`DIVIDEND`. Le cours baisse au détachement, le dividende revient en cash : ça se compense dans le
P/L final. **Je ne propose aucun changement de ce modèle.**

Le constat est plus étroit : **une des deux voies d'écriture ne le respecte pas.**

| Voie | Ce qu'elle écrit |
|---|---|
| Cron nocturne `update_all_prices_daily` (`services/market.py:697`) | **Cours brut** — conforme au modèle |
| Backfill `_backfill_stock_prices` → `ticker.history()` (`services/market_data/providers/yahoo.py:260`) | **Cours ajusté** — yfinance a `auto_adjust=True` par défaut, donc dividendes et splits réintégrés |

Sur une plage backfillée d'un actif **distribuant**, le cours inclut déjà le dividende, et le
`DIVIDEND` saisi s'ajoute par-dessus : double comptage. La correction serait un `auto_adjust=False`
dans `get_historical_prices`, mais elle change la sémantique des lignes déjà stockées. **Hors
périmètre — ticket séparé, à toi de trancher.**

**Portée réelle du problème.** Il ne se manifeste que sur les actifs **distribuants**. Un ETF
**capitalisant** ne distribue rien : ajusté et brut sont identiques, et son cours *est* déjà le
rendement total. Pour un portefeuille d'ETF capitalisants — le cas le plus probable ici — le
problème est nul.

*Conséquence pour ce design* : aucune. Je contrainds simplement le benchmark à être un **ETF
capitalisant** (§0), ce qui rend sa série exacte sans traitement particulier. Pour le portefeuille,
je m'appuie sur les snapshots `AccountHistory` existants — cohérent avec ce que les graphes de l'app
affichent déjà. Une divergence entre ma page et tes courbes actuelles détruirait la confiance plus
sûrement qu'une imprécision assumée. La note de méthode signale le cas des lignes distribuantes.

### 7.2 · Les dépôts automatiques sont des écritures, pas des décisions

`create_stock_transaction` (`services/stock_transaction.py:308-327`) crée un dépôt EUR automatique
1 seconde avant un `BUY` quand le cash est insuffisant, avec la note `"Provision automatique"`.

Ces lignes n'ont aucune valeur comportementale : elles sont datées sur l'achat, pas sur le virement
réel. Traitement retenu (§0 bis) : **exclusion de toute statistique**, y compris de la série de flux
du MWR. Leur part est calculée et affichée, car elle borne ce qu'on peut dire du §2.4 : si 80 % des
dépôts sont synthétiques, la date réelle d'entrée de l'argent est inconnue et le décalage
dépôt→investissement n'est mesurable que sur le solde restant.

---

## 8. Architecture technique

### Backend — `capitalview-api`

```
services/analytics/
├── __init__.py
├── flows.py          — flux externes journaliers + appariement FIFO dépôt→achat (source unique, R1)
├── returns.py        — TWR journalier chaîné, XIRR par bissection, annualisation gatée
├── benchmark.py      — résolution et récupération de la série benchmark
├── counterfactual.py — moteurs de rejeu : robot, substitutions séquentielles
├── execution.py      — slippage par ordre vs TWAP de période
├── timing.py         — conditionnement au marché + tests de permutation (mutualisés)
├── concentration.py  — HHI, N effectif, ACP / entropie de Meucci
├── behaviour.py      — régularité des achats, HHI temporel, décalage dépôt→achat, PGR/PLR
├── fees.py           — seuils, distribution, projection
├── plan.py           — adhérence au plan (no-op si plan absent)
└── report.py         — assemblage du DTO, statuts de fiabilité, rédaction des verdicts

routes/analytics.py   — GET /analytics/investor
dtos/analytics.py     — DTOs typés, un par bloc
```

**Un seul endpoint**, parce que tous les blocs partagent le même rejeu (flux journaliers, matrice de
prix, série benchmark). Le découper en 6 appels triplerait le coût de calcul.

**Cache** : mémo côté serveur clé sur `max(updated_at)` des transactions du user + date du jour ;
côté client, TTL 1 h via `getOrFetchCached` — le pattern est déjà en place dans `stores/stocks.ts`.

Les fonctions de calcul sont **pures** (entrée : listes de transactions décryptées + séries de prix ;
sortie : nombres). Testables sans base ni réseau.

### Frontend — `capitalview-web`

```
pages/Analysis.vue
stores/analysis.ts
components/analytics/
├── VerdictBanner.vue
├── ReliabilityBadge.vue         — le composant qui matérialise le §2, réutilisé partout
├── AttributionWaterfall.vue     — ECharts, le pont contrefactuel
├── ContributionHeatmap.vue      — heatmap calendaire
├── SlippageDistribution.vue     — box plot + histogramme
├── MarketStateScatter.vue       — nuage date × drawdown, taille = €
├── DensityComparison.vue        — deux densités superposées
└── CorrelationMatrix.vue        — heatmap de corrélation + N effectif
```

ECharts 6 + vue-echarts sont déjà les dépendances de charts du projet, et tous ces types de graphes
y sont couverts. Aucune dépendance nouvelle, ni back ni front.

Route `/analyse` + entrée de nav dans `DefaultLayout.vue` (icône `Microscope`, lucide déjà présent).

### Migrations

Une seule, sur `user_settings` :
- `benchmark_asset_key` (TEXT, nullable, défaut applicatif = ETF MSCI World)
- `investment_plan_enc` (TEXT, nullable, JSON chiffré : montant mensuel + allocation cible)

Le plan est chiffré comme le reste des données financières — cohérence zéro-knowledge.

**Pas de migration sur `market_assets`.** Le benchmark est un vrai ETF : il vit comme un
`MarketAsset` de type `STOCK` normal, alimenté par le backfill existant.

---

## 9. Refactors nécessaires — minimaux, non destructifs

**R1 · Source unique pour les flux externes — seul refactor nécessaire.**
`_compute_daily_net_flow` (`services/account_history.py:330`) travaille sur un `_AccountSnapshot`.
J'extrais la branche STOCK en une fonction pure `stock_external_flows(transactions) -> dict[date, Decimal]`
dans `analytics/flows.py`, et `account_history.py` l'appelle. Une seule définition du flux externe
dans le codebase, sinon TWR et snapshots divergeront un jour. Couvert par les tests existants de
`test_account_history.py`.

La fonction expose le filtrage des provisions automatiques en option (`include_auto_provisions`),
laissé à `True` pour `account_history` (comportement inchangé) et mis à `False` par l'analytics.

**Le flag `force` sur le backfill n'est plus nécessaire** : la contrainte « benchmark = ETF
capitalisant » (§7.1) rend la série exacte sans traitement particulier.

Rien d'autre n'est touché. Aucune modification de `Stock.vue`, des services stock existants, ni du
calcul des 8 encarts.

---

## 10. Découpage en jalons

| Jalon | Contenu | Valeur livrée |
|---|---|---|
| **M1** | R1, `benchmark.py`, `flows.py`, `returns.py`, cadre de fiabilité, page + store + `ReliabilityBadge` | §1.1 en ligne : l'écart investisseur, chiffré en euros |
| **M2** | `counterfactual.py`, `execution.py`, `timing.py` (permutations mutualisées) | §1.2 et §1.3 : le pont contrefactuel et le coût d'exécution |
| **M3** | `behaviour.py`, `concentration.py`, `fees.py`, `plan.py`, verdicts | §2.1 à §3.2 (dont §2.4) + le bloc plan |

Chaque jalon est autonome et livrable.

---

## 11. Tests et garanties de correction

- **Réconciliation du waterfall** : la somme des termes doit égaler la valeur réelle du portefeuille
  à l'euro près. Test bloquant.
- **TWR vs MWR sur cas connus** : un portefeuille sans flux intermédiaires ⇒ TWR = MWR exactement.
  Un versement unique en fin de période ⇒ écart de signe prévisible. Cas construits à la main.
- **XIRR** : validé contre des valeurs de référence, y compris flux irréguliers et rendement négatif.
- **Tests de permutation** : sur données synthétiques à biais nul, le p-value doit être
  approximativement uniforme sur [0,1] (test sur 200 tirages de jeux synthétiques).
- **Gates de fiabilité** : chaque seuil a un test qui vérifie que la métrique bascule bien en
  `insuffisant` et que **la valeur n'est pas sérialisée** dans la réponse.
- **Non-régression** : la suite existante (`tests/services/test_stock_*.py`,
  `test_account_history.py`) doit passer inchangée après R1.

---

## 12. Risques

| Risque | Mitigation |
|---|---|
| Les dépôts auto-provisionnés dominent ⇒ le §2.4 devient peu mesurable | Exclus de toute stat, part affichée, §2.4 gaté sur le solde réel (§0 bis, §7.2). Les métriques de discipline reposent sur les **achats** et ne sont pas affectées. |
| Trop peu d'opérations pour conclure quoi que ce soit | C'est le cadre de fiabilité, pas un accident : la page dira « je ne sais pas » là où c'est le cas |
| Temps de calcul (permutations × 5 000, ACP, rejeux) | Un seul endpoint, cache serveur + client, numpy déjà tiré par les dépendances existantes |
| Ajustement dividendes du backfill sur une ligne distribuante | Sans effet sur un ETF capitalisant (benchmark contraint, §7.1) ; signalé en note de méthode pour les lignes distribuantes |
| La page devient un dashboard de 30 chiffres | 9 métriques, hiérarchisées, avec une section explicite de ce qui est écarté |

---

## 13. Références

- Dichev, I. (2007). *What Are Stock Investors' Actual Historical Returns? Evidence from
  Dollar-Weighted Returns.* American Economic Review 97(1).
- Morningstar. *Mind the Gap* (étude annuelle sur l'écart rendement du fonds / rendement de
  l'investisseur).
- Odean, T. (1998). *Are Investors Reluctant to Realize Their Losses?* Journal of Finance 53(5).
- Barber, B. & Odean, T. (2000). *Trading Is Hazardous to Your Wealth.* Journal of Finance 55(2).
- Perold, A. (1988). *The Implementation Shortfall: Paper versus Reality.* Journal of Portfolio
  Management.
- Brinson, G., Hood, R. & Beebower, G. (1986). *Determinants of Portfolio Performance.* Financial
  Analysts Journal.
- Meucci, A. (2009). *Managing Diversification.* Risk 22(5).
- Choueifaty, Y. & Coignard, Y. (2008). *Toward Maximum Diversification.* Journal of Portfolio
  Management.
- Greenwood, R. & Shleifer, A. (2014). *Expectations of Returns and Expected Returns.* Review of
  Financial Studies.
- Frazzini, A. & Lamont, O. (2008). *Dumb Money: Mutual Fund Flows and the Cross-Section of Stock
  Returns.* Journal of Financial Economics.
- GIPS (Global Investment Performance Standards) — exigence de rendement time-weighted.
