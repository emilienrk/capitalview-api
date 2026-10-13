# Le tri des opérations bancaires

Comment chaque opération d'un compte bancaire est comptée : virement entre comptes, dépense, épargne, investissement,
ou non compté. Règles validées par Emilien les 8 et 9 octobre 2026. Tableau d'origine, avec l'avis argumenté sur
chaque règle : https://claude.ai/artifact/9TBgxbN7UCE9iM7rCSSQM2

## La règle

**Par défaut, l'appli ne fait aucun faux positif.** Elle n'automatise que ce qui repose sur une preuve : une décision
de l'utilisateur, la loi, ou une répétition mesurée. **Jamais un mot du libellé** : les libellés ne sont pas
standardisés, ils changent d'une banque et d'une langue à l'autre, et un même libellé couvre des opérations de natures
différentes (« VIR Virement interne » vers un livret, vers un ami, vers un PEA).

Quand il n'y a pas de preuve, l'opération est comptée comme le dit la banque, et l'incertitude se voit. Les règles
que l'utilisateur écrit lui-même (« contient », « commence par ») sont sa parole, pas une devinette : elles ne sont
derrière aucune option, rien ne se passe tant qu'il n'en écrit pas (décidé le 10/10). Les mots appris sont abandonnés.

Chaque changement de règle se mesure sur un dump réel, avant et après : paires, types, questions, totaux.

## Ce qui est automatique

| Règle | Exemple | Pourquoi c'est sûr |
| --- | --- | --- |
| **Ta décision** : lier deux opérations, ou les délier | tu lies −200 € Boursorama et +200 € Revolut | c'est toi qui l'as dit |
| **Livret réglementé** (Livret A, LDDS, LEP, PEL, CEL) d'un côté d'une paire | courant −500 € → Livret A +500 € le même jour | la loi interdit qu'il aille ailleurs que vers ton compte |
| **Paire répétée** : même débit, même crédit, mêmes deux comptes, au moins 3 fois | tes virements Boursorama ↔ Revolut | mesuré : aucun faux virement à 3, un remboursement de tiers passait à 2 |
| **Paire déjà validée** : les deux côtés ressemblent aux deux côtés d'une paire que tu as validée, sur les mêmes comptes | une recharge Revolut validée une fois, les suivantes passent | ta validation porte sur *cette paire*, pas sur tous les virements d'un des deux noms |
| **Remboursement sur un même compte** : même montant dans les 30 jours, avec un mot rare commun | −30 € Amazon, puis +30 € Amazon | les mots trop fréquents sont calculés sur ton compte, sans dictionnaire |
| **Dépôt Bourse/Crypto déjà validé** : même jour, même montant au centime ou un écart de frais (1 € ou 2 % au plus, chacun le seul candidat de l'autre ce jour-là), vers le même compte d'investissement qu'un dépôt auquel tu as répondu « oui » pour ce libellé, depuis ce compte | ton virement mensuel vers le PEA | comme une paire déjà validée ; la leçon se tire de tout l'historique, avant comme après ta réponse |

Une paire reconnue n'est **jamais** une règle par nom : elle ne classe pas les autres opérations qui portent un des deux
libellés. Une opération n'a qu'une paire, et un dépôt ne justifie qu'une seule opération.

## Ce qui reçoit une question

Une question n'est posée que lorsque **quelque chose est en face**, ou qu'une sortie **dépasse le seuil** choisi par
l'utilisateur.

- **Paire possible** entre deux comptes (sens opposés, même montant, au plus 2 jours ouvrés d'écart), qu'aucune règle
  ci-dessus ne confirme. Y compris entre deux comptes courants, même avec le même libellé, le même jour et le même
  montant. Une paire seulement proposée ne change aucun total.
- **Dépôt Bourse/Crypto possible** : même montant, dans les 3 jours, ou avec un écart de frais. La question est
  « est-ce celui-ci ? », Oui / Non, sur chaque opération concernée (`services/banking/contribution_decisions.py`) :
  - **Oui** lie l'opération à ce dépôt, des deux côtés, et la compte en investissement ; il vaut pour le libellé
    (ligne « Dépôt déjà validé » plus haut). Un type choisi à la main sur cette opération s'efface : ce qu'elle est
    décide de comment elle compte. Elle ne rejoint plus aucune paire, proposée ou automatique : seul « Lier » peut
    encore l'apparier, et il efface le Oui, comme un type choisi à la main ensuite. Une seule décision par
    opération. L'opération d'en face libérée compte seule (une entrée pose sa question à partir de 100 €).
  - **Non** la sort du rapprochement : elle se lit comme toute autre opération, et le dépôt peut faire face à une
    autre opération.
  - Dans « À trier », ces opérations forment leur propre groupe par compte, libellé et compte d'investissement (décidé
    le 13/10), à part de la question de type du libellé. La question s'affiche sur la plus récente d'entre elles :
    Oui / Non pour celle-ci, ou « Choisir » pour répondre aux cochées, chacune avec son propre dépôt. Un libellé qui
    change demande un Oui par libellé. Mesuré sur le dump : les 17 « VIR Virement depuis Compte courant » portées par
    un virement de 500 € sans rien en face deviennent 16 face au PEA et 1 question de type ; même montant à trier.
    Ces questions passent en tête d'« À trier », le reste suit par montant (décidé le 13/10) : un Oui apprend la
    forme et range seules les opérations suivantes qui la suivent.
  - Une réponse tient tant que les deux côtés se font encore face : un dépôt supprimé ou déplacé depuis la laisse
    sans effet. Ni un type choisi, ni une règle n'apprennent un dépôt : une règle parle d'un libellé, et un libellé
    peut aller au PEA un jour et ailleurs le lendemain.
  - Mesuré sur le dump (12/10) : 7 « oui », un par forme, rangent 58 des 59 sorties face à un dépôt le même jour (la
    dernière attend la réponse sur sa paire proposée), et chaque page mensuelle lit comme l'historique entier. Avant,
    la leçon ne voyait que les mois chargés par la page : 45 sorties comptaient en dépense sur leur mois.
  - Si une paire et un dépôt sont tous deux possibles, la même question montre les deux : « Ensemble » ou
    « versement sur <compte d'investissement> » (le Oui, qui écarte la paire). Tant que la paire est proposée, le
    dépôt n'est qu'affiché, même déjà validé pour ce libellé. Mesuré sur le dump : un cas (−20 € du
    05/02/2024), dont le dépôt s'affichait jusque-là, à tort, sur une autre sortie de 20 € deux jours plus tard.
- **Sortie sans rien en face, au-dessus du seuil** du curseur (500 € par défaut, réglable). Le seuil porte sur
  l'opération, pas sur le total du libellé. Les questions restent regroupées par libellé, « celle-ci » par défaut.
  Mesuré sur un historique de 4 ans : 6 opérations à 1 000 €, 20 à 500 €, 54 à 250 €, 139 à 100 €. À 100 €, on
  retrouve à peu près les questions d'avant le 09/10, cartes comprises : sans dictionnaire, un achat carte et un
  virement ne se distinguent pas.
- **Entrée sans rien en face** (« revenu ou remboursement ? »), groupée par nom, si le groupe dépasse 100 €. Seul
  l'utilisateur sait si les 50 € d'un ami sont un remboursement.

Les questions sont rangées par année, les plus récentes d'abord.

## Ce qui ne reçoit pas de question

**Une sortie sans rien en face, sous le seuil**, est une dépense : si tous les comptes d'épargne et d'investissement
sont dans l'appli, elle ne peut être que ça. Le curseur sert à ça : en le bougeant, on voit combien de sorties
passeraient en question, et on attrape un compte oublié (3 000 € partis vers un PEA ouvert ailleurs). Autour :

- dans l'onglet Comptes, dès qu'il y a un compte, une phrase qu'on peut fermer (fermée pour de bon sur ce
  navigateur) : « Ajoutez aussi vos livrets et vos comptes de bourse : sinon, les virements vers eux compteront comme
  des dépenses. » ;
- à côté des totaux du Réel : « X € à trier (N opérations) », avec un lien vers « À trier ».

**Les dépôts sans contrepartie** sont listés aussi, sous les questions d'« À trier » : les dépôts (et les retraits)
déclarés sur un compte Bourse, Crypto ou un placement pour lesquels aucune opération bancaire n'a été trouvée. C'est
l'inverse du curseur : il attrape un compte d'investissement oublié, cette liste attrape un compte bancaire oublié. Ils
ne reçoivent pas de question et ne changent aucun total.

- Est « en face » toute opération de sens opposé, même montant ou un écart de frais, à 3 jours au plus, quel que soit
  son type. Une paire déjà reconnue entre deux comptes bancaires ne compte pas ; une paire seulement proposée, si.
- Un pour un : une sortie de 200 € ne couvre pas deux dépôts de 200 €.
- Seuls les dépôts compris dans l'historique bancaire sont jugés, et pas les 3 derniers jours, où l'autre côté peut
  ne pas être encore passé.
- Mesuré sur le dump : 64 dépôts déclarés, 3 sans contrepartie (500 € sur un compte « trst », 90,17 € et 180 € sur le
  portefeuille crypto).

## Répondre

- **Celle-ci** : le choix par défaut.
- **Celles que je coche** : parmi les opérations du même nom, listées (« Choisir parmi les N opérations de ce
  libellé »). Liste ouverte, la réponse porte sur les opérations cochées, celle de la question cochée d'avance ;
  fermée, sur celle-ci. Chacune reçoit son propre type, comme si on avait répondu une par une : aucune règle, rien
  pour les prochaines. Seules des opérations du même compte et du même sens peuvent être cochées ensemble.
- **Les prochaines aussi** : case explicite, qui crée une règle. Les règles sont le dictionnaire de l'utilisateur :
  onglet « Règles » de Banque (Paramètres › Banque jusqu'au 13/10), chacune avec son libellé, son compte, son sens
  et le nombre d'opérations qu'elle type ; on y change son type ou on la supprime. Validé le 10/10 : une règle parle des
  opérations à venir, elle n'a pas sa place dans l'historique des réponses.
- Une règle par nom n'empêche **jamais** une paire trouvée plus tard : la paire passe avant.
- **Une règle écrite à la main**, bouton « Nouvelle règle » de l'onglet « Règles » : le libellé « contient » ou
  « commence par » un texte, sur un compte ou sur tous (ceux ajoutés plus tard compris), en entrées ou en sorties, et
  le type. Jamais de regex : le texte est pris tel quel, casse, accents et espaces répétés repliés ; 3 caractères au
  moins. L'aperçu est obligatoire avant d'enregistrer : les opérations qu'elle comptera et combien changent de type,
  les règles à qui elle en prend et celles, plus précises, qui gardent les leurs (nommées), ce qu'une paire ou un
  dépôt garde, et un avertissement quand elle touche plus de la moitié des opérations de ce côté. Toute règle, même
  née d'une réponse, peut être réécrite ainsi ; la même règle écrite deux fois remplace la première.
- **Un choix fait à la main l'emporte toujours sur une règle.** L'aperçu liste ceux d'un autre type que la règle (les
  autres ne disent rien) ; une case, décochée par défaut, « Les passer aussi en … », retire ces choix et les donne à
  la règle. Les paires ne sont jamais concernées. Validé le 10/10.
- Dans l'autre sens, un choix à la main ou une règle née d'une réponse qui passe désormais avant une règle écrite le
  dit dans son message (« Pour ce libellé, elle passe avant la règle Contient « amazon » »).
- Entre deux règles : celle née d'une réponse (ce libellé-là, et les libellés proches) passe avant une règle écrite ;
  entre règles écrites, le texte le plus long, puis celle d'un seul compte, puis la plus récente.

## Délier, annuler

- Une paire proposée se pose en toutes lettres : « C'est… Ensemble / Pas ensemble ». On ne sait pas si c'est un
  virement entre ses comptes ou un tiers qui rembourse un achat sur l'autre compte (presque toutes les paires proposées
  du dump, le 12/10) : le badge dit « avec <compte> ? », sans flèche, et la ligne dessous dit seulement ce que fait
  chaque réponse — « Ensemble : elles s'annulent, aucune n'est comptée » (ou « comptées en épargne » face à un compte
  épargne non réglementé, `together_type`), « Pas ensemble : chacune restera comptée de son côté ». Une fois liée, la
  paire reprend sa flèche « vers / depuis <compte> ».
- Un refus ne vaut que pour **sa** paire. Il n'apprend rien sur les libellés. (Avant le 08/10, un seul refus bloquait
  toutes les paires aux libellés semblables, livrets compris : 8 virements vers le Compte plaisir perdus.)
- **Historique de toutes les actions** (paires liées et déliées, réponses), chacune annulable ; les règles ont leur page. Une paire
  reconnue après coup ne remplace jamais en silence un type choisi à la main.
  - « Historique de mes réponses », en bas d'« À trier » : chaque décision encore en vigueur (paires liées, refusées,
    annulations ; réponses sur un dépôt ; types choisis sur une opération ; récurrents confirmés ou refusés), la plus
    récente d'abord. Validé le 09/10 : lue là où chaque décision est déjà rangée, pas dans un journal à part.
    « Annuler » supprime la décision, donc tout revient exactement comme avant ; une décision annulée quitte la liste.
  - Un type choisi à la main garde sa date (`type_override_at`, vide pour ceux d'avant le 09/10).
  - Quand une paire reconnue depuis passe avant un type choisi à la main, l'historique le dit sur ce choix. Sur le
    dump du 25/09, aucun type choisi à la main ni décision de paire : rien à mesurer.

## Ordre de priorité d'un type

Ajustement de solde > paire reconnue > choix sur cette opération > dépôt prouvé > règle (née d'une réponse, puis
écrite) > récurrent > défaut (entrée = revenu, sortie = dépense).

Une paire avec exactement un compte d'épargne compte en Épargne ; entre deux comptes courants, ou entre deux épargnes,
elle n'est pas comptée.

## Ce qui ne décide jamais

Le moyen de paiement (« CARTE », « VIR », « PRLV »…) n'est plus lu du tout, ni affiché ni filtré (retiré le 12/10) :
le libellé le dit déjà, rien ne se décidait dessus, et son dictionnaire dépendait de la banque. Les mots ignorés pour
grouper les marchands servent à l'affichage, pas à décider d'un regroupement de réponses.

## Récurrents (second temps)

Détection sans dictionnaire, par marchand, rythme et montant : un prix qui change, un nom qui change, une pause restent
le même récurrent. Les couches actuelles, mesurées sur des cas réels, sont gardées. Mesurer sur le dump avant de changer
quoi que ce soit.

**Rien n'est compté seul** (décidé le 10/10) : toute série détectée est proposée dans l'onglet Récurrents, et ne compte
qu'une fois confirmée ; ses remboursements liés aussi. Détecter large : une fausse proposition ne coûte qu'un « non », un
récurrent raté coûte en silence. Un loyer ou de l'argent de poche détecté est acceptable. Mesuré sur le dump : les 25
séries restent, les 15 qui comptaient seules (« auto ») deviennent des propositions ; les charges fixes passent de
686,88 € à 624,56 € par mois (les 5 confirmées) et les revenus récurrents de 251,27 € à 0 € jusqu'à validation ;
« À trier » ne bouge pas.

**Pas de seuil dans les récurrents** : une dépense proposée n'apparaît que dans Récurrents, pas dans « À trier ».
Refusée, ses opérations au-dessus du seuil tombent dans « À trier ».

**Un dépôt en face, le même jour, passe devant le récurrent** (décidé le 13/10) : l'opération pose sa question « est-ce
celui-ci ? » dans « À trier », comme sous une règle. Oui = comptée en investissement et sortie de la détection des
récurrents (un récurrent fait de ces seules opérations disparaît de l'onglet, même confirmé ; il reste dans
l'historique, et revient si le Oui est annulé). Non = elle reste dans son récurrent. Seul le Oui sort une opération des
récurrents : ni une règle ni un type choisi à la main. Mesuré sur le dump : aucun cas, rien ne bouge.

**Aucun moyen de paiement** (décidé le 10/10) : carte, prélèvement ou virement, on ne les lit pas, et aucun mot n'est
appris. La ressemblance des libellés (`merchants.py`) reste telle quelle : un abonnement garde la même forme de
libellé. Une seule règle pour toutes les dépenses (`recurrence.offered`) :

- un rythme régulier (mensuel, 4 semaines, bimestriel, trimestriel, semestriel, annuel), à 3 jours près, les deux
  tiers des échéances au moins ;
- un montant fixe au centime entre ses hausses (deux pas sur trois au centime : MACIF et ses 5 hausses en 24
  prélèvements), ou stable à 3 % s'il n'est pas rond (un abonnement payé en devise) ; 3 échéances, 4 pour un montant
  rond (3 pizzas à 12 € à un mois d'écart sur le dump), 6 sous 2 € ;
- ou un montant variable (une facture d'énergie, de téléphone) : mensuel ou trimestriel, 4 échéances, chez un
  marchand qui ne reçoit presque rien d'autre (les trois quarts) ; tous les deux mois, un coiffeur et une pizzeria
  passaient ;
- le marchand reçoit surtout la série (70 %) : sinon c'est un magasin où l'on achète aussi ;
- chaque semaine ou toutes les deux semaines (un ménage, de l'argent de poche), le marchand ne reçoit presque rien
  d'autre (90 %) : sur le dump, deux machines à café en recevaient les trois quarts ;
- une cotisation annuelle vue deux fois : à un dixième près (révisée), pas ronde, 10 € au moins.

Les intérêts ne passent plus par le mot « INTER » : un crédit sans paire sur un livret réglementé (le type du compte)
ne peut venir que de la banque, une fois l'an. Un renommage (EDF, OVH) se reconnaît au même montant au centime, sur le
même compte, à l'échéance suivante. Les retraits ne sont plus écartés d'avance : sans régularité, ils ne forment pas de
série. Un paiement par virement prend ses remboursements comme les autres, une fois confirmé.

Mesuré sur le dump du 25/09 : les mêmes 25 séries, EDF, Olness et les intérêts compris (sans dictionnaire, l'ancienne
règle carte les perdait : un montant rond devait tomber au jour près), aucune nouvelle ; « À trier » ne bouge pas. Un
effet : le loyer confirmé « Frederic Durand », payé par virement, prend comme remboursement le crédit de 345 € du
29/07/2026 venu du même nom.

## État au 9 octobre 2026

| Point | État |
| --- | --- |
| Un refus ne vaut que pour sa paire | poussé (6a53229) |
| Règle « carte face à un virement reçu » (dictionnaire) | retirée |
| Libellé identique automatique, dépôt rapproché même pour une carte (session du 08/10) | défaits, jamais poussés |
| Réponse « celle-ci » par défaut, case « toutes celles de ce libellé, et les prochaines » | fait |
| Une sortie n'est demandée que lorsqu'un dépôt ou un retrait lui fait face | fait |
| Dépôt du même jour : question, puis automatique pour le même libellé vers le même compte | fait |
| Dépôt : Oui / Non stockés des deux côtés, leçon tirée de tout l'historique (12/10) | fait |
| « Celles que je coche » dans la liste | fait |
| Sortie sans contrepartie au-dessus du seuil = question, curseur (500 € par défaut, onglet « Règles » de Banque) | fait |
| Dépôts sans contrepartie, « X € à trier », phrase à l'ajout d'une banque | fait |
| Paire proposée et dépôt possibles en même temps : une seule question | fait |
| Texte du bouton : « Pas ensemble », question écrite sur la ligne | fait |
| Historique et annulation | fait |
| Page « Règles de tri » (les règles sortent de l'historique) | fait |
| Règles écrites à la main, « contient » / « commence par », avec aperçu, sans option | fait |
| Récurrents : rien compté seul, toute série proposée | fait |
| Récurrents sans dictionnaire ni moyen de paiement, une seule règle | fait |
| Moyen de paiement retiré partout, affichage et filtre compris | fait |
