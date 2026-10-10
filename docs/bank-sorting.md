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
| **Dépôt Bourse/Crypto déjà validé** : même jour, même montant au centime, vers le même compte d'investissement qu'un dépôt que tu as validé avec ce libellé | ton virement mensuel vers le PEA | comme une paire déjà validée |

Une paire reconnue n'est **jamais** une règle par nom : elle ne classe pas les autres opérations qui portent un des deux
libellés. Une opération n'a qu'une paire, et un dépôt ne justifie qu'une seule opération.

## Ce qui reçoit une question

Une question n'est posée que lorsque **quelque chose est en face**, ou qu'une sortie **dépasse le seuil** choisi par
l'utilisateur.

- **Paire possible** entre deux comptes (sens opposés, même montant, au plus 2 jours ouvrés d'écart), qu'aucune règle
  ci-dessus ne confirme. Y compris entre deux comptes courants, même avec le même libellé, le même jour et le même
  montant. Une paire seulement proposée ne change aucun total.
- **Dépôt Bourse/Crypto possible** : même montant, dans les 3 jours, ou avec un écart de frais. Si une paire et un
  dépôt sont tous deux possibles, la même question montre les deux : « C'est… vers <compte> » ou « versement sur
  <compte d'investissement> » (la paire écartée, l'opération comptée en investissement). Tant que la paire est
  proposée, le dépôt n'est qu'affiché, même déjà validé pour ce libellé. Mesuré sur le dump : un cas (−20 € du
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
  page « Règles de tri » de Paramètres › Banque, chacune avec son libellé, son compte, son sens et le nombre
  d'opérations qu'elle type ; on y change son type ou on la supprime. Validé le 10/10 : une règle parle des
  opérations à venir, elle n'a pas sa place dans l'historique des réponses.
- Une règle par nom n'empêche **jamais** une paire trouvée plus tard : la paire passe avant.
- **Une règle écrite à la main**, bouton « Nouvelle règle » de la page « Règles de tri » : le libellé « contient » ou
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

- Le bouton qui refuse une paire dit ce qu'il fait : « Pas ensemble », et dessous « chacune restera comptée de son
  côté ». Il ne veut pas dire « compter en dépense ». La paire proposée est posée en toutes lettres sur la ligne
  (« C'est… vers <compte> / Pas ensemble »), plus par deux icônes ✓ ✗.
- Un refus ne vaut que pour **sa** paire. Il n'apprend rien sur les libellés. (Avant le 08/10, un seul refus bloquait
  toutes les paires aux libellés semblables, livrets compris : 8 virements vers le Compte plaisir perdus.)
- **Historique de toutes les actions** (paires liées et déliées, réponses), chacune annulable ; les règles ont leur page. Une paire
  reconnue après coup ne remplace jamais en silence un type choisi à la main.
  - « Historique de mes réponses », en bas d'« À trier » : chaque décision encore en vigueur (paires liées, refusées,
    annulations ; types choisis sur une opération ; récurrents confirmés ou refusés), la plus
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

Le dictionnaire de moyens de paiement (« CARTE », « VIR », « PRLV »…, `services/banking/operation_types.py`) ne décide
plus rien : ni quelle paire est proposée, ni quelle opération reçoit une question, ni quel dépôt est rapproché. Il
peut rester pour l'affichage. Les mots ignorés pour grouper les marchands servent à l'affichage, pas à décider d'un
regroupement de réponses.

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

## État au 9 octobre 2026

| Point | État |
| --- | --- |
| Un refus ne vaut que pour sa paire | poussé (6a53229) |
| Règle « carte face à un virement reçu » (dictionnaire) | retirée |
| Libellé identique automatique, dépôt rapproché même pour une carte (session du 08/10) | défaits, jamais poussés |
| Réponse « celle-ci » par défaut, case « toutes celles de ce libellé, et les prochaines » | fait |
| Une sortie n'est demandée que lorsqu'un dépôt ou un retrait lui fait face | fait |
| Dépôt du même jour : question, puis automatique pour le même libellé vers le même compte | fait |
| « Celles que je coche » dans la liste | fait |
| Sortie sans contrepartie au-dessus du seuil = question, curseur (500 € par défaut, Paramètres › Modules) | fait |
| Dépôts sans contrepartie, « X € à trier », phrase à l'ajout d'une banque | fait |
| Paire proposée et dépôt possibles en même temps : une seule question | fait |
| Texte du bouton : « Pas ensemble », question écrite sur la ligne | fait |
| Historique et annulation | fait |
| Page « Règles de tri » (les règles sortent de l'historique) | fait |
| Règles écrites à la main, « contient » / « commence par », avec aperçu, sans option | fait |
| Récurrents : rien compté seul, toute série proposée | fait |
| Récurrents sans dictionnaire | second temps |
