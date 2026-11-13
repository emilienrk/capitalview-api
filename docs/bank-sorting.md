# Le tri des opérations bancaires

Comment chaque opération d'un compte bancaire est comptée : virement entre comptes, dépense, épargne, investissement,
ou non compté. Ce document est la référence : le code la suit, et chaque changement de comportement le met à jour.

Il se lit en trois parties. **Les règles**, numérotées R1 à R54, stables, chacune avec le module qui la porte : c'est
ce qu'une session de développement doit connaître avant de toucher `services/banking`. **Les textes d'interface**,
tels qu'ils s'affichent. **Le journal**, daté : chaque décision, chaque mesure sur le dump de développement, chaque
commit, dans l'ordre. Rien de l'ancienne version n'a été retiré : ce qui n'est plus une règle est dans le journal.

Règles validées par Emilien du 8 au 31 octobre 2026, complétées le 2 novembre. Tableau d'origine, avec l'avis
argumenté sur chaque règle : https://claude.ai/artifact/9TBgxbN7UCE9iM7rCSSQM2

---

## Les règles

### Principe

**R1. Par défaut, l'appli ne fait aucun faux positif.** Elle n'automatise que ce qui repose sur une preuve : une
décision de l'utilisateur, la loi, ou une répétition mesurée. **Jamais un mot du libellé** : les libellés ne sont pas
standardisés, ils changent d'une banque et d'une langue à l'autre, et un même libellé couvre des opérations de natures
différentes (« VIR Virement interne » vers un livret, vers un ami, vers un PEA).

**R2. Sans preuve, l'opération est comptée comme le dit la banque**, entrée = revenu, sortie = dépense, et
l'incertitude se voit (`cashflow_types.resolve_type`, source `default`).

**R3. Les règles que l'utilisateur écrit lui-même sont sa parole, pas une devinette** : elles ne sont derrière aucune
option, rien ne se passe tant qu'il n'en écrit pas. Aucun mot n'est appris d'un libellé.

**R4. Chaque changement de règle se mesure sur un dump réel, avant et après** : paires, types, questions, totaux.
Les mesures sont dans le journal.

**R5. Un dépôt déclaré est validé, demandé ou listé, jamais couvert sans un mot.** Validé : un Oui, ou la preuve du
même jour pour une forme déjà validée. Demandé : une question Oui / Non sur l'opération en face. Listé : « Sans
contrepartie bancaire ». Une seule règle dit ce qui est « en face », pour la question, l'alerte et la tenue des
réponses : `contributions.fits`. Il n'y en a pas une plus large pour l'alerte.

**R6. Un écart de date se compte partout en jours ouvrés** du calendrier TARGET2 (`services/banking/settlement.py`),
paires comme dépôts : 2 au plus, et jamais plus de 10 jours de calendrier. Seul « Lier » à la main accepte jusqu'à
28 jours de calendrier (`transfer_decisions.MAX_DECISION_DAYS`), parce que tout lecteur charge le mois de chaque côté
du sien.

### Ce qui est automatique

Chaque ligne dit pourquoi c'est sûr. Le code : `flows._internal_transfer_legs`, `flows._transfer_status`.

**R7. Ta décision** : lier deux opérations, ou les délier. Exemple : tu lies −200 € Boursorama et +200 € Revolut.
C'est toi qui l'as dit (`services/banking/transfer_decisions.py`).

**R8. Livret réglementé** (Livret A, LDDS, LEP, PEL, CEL) d'un côté d'une paire. Exemple : courant −500 € → Livret A
+500 € le même jour. La loi interdit qu'il aille ailleurs que vers ton compte. Mais pas laquelle de deux opérations aussi proches c'est : quand une autre opération aussi
proche, d'un autre compte ou d'un autre libellé, et encore libre, peut être l'autre jambe, la paire est seulement
proposée, avec l'alternative (R34). Deux opérations identiques (même compte, même libellé) ne sont pas une
alternative : n'importe laquelle est juste. Une paire contestée dont toutes les rivales ont trouvé leur propre paire
n'est plus en doute et se valide. Le type générique « Épargne » n'a pas cette règle (R38).

**R9. Paire répétée** : même débit, même crédit, mêmes deux comptes, mêmes deux signatures de libellé, au moins 3 fois
dans l'historique (`transfer_patterns.RECURRING_MIN_OCCURRENCES`). Exemple : tes virements Boursorama ↔ Revolut. *Mesuré : aucun faux virement à 3, un remboursement
de tiers passait à 2, 4 n'en manquait que plus.*

**R10. Paire déjà validée** : les deux côtés ressemblent (0,6 de mots en commun, `labels.SIMILARITY_THRESHOLD`) aux
deux côtés d'une paire que tu as liée, sur les mêmes comptes et sens. Exemple : une recharge Revolut validée une fois,
les suivantes passent. Ta validation porte sur *cette paire*, pas sur tous les virements d'un des deux noms. *Mesuré en rejouant quatre ans de décisions mois par mois : plus bas, un « VIR
INST <quelqu'un d'autre> » passait pour le « VIR INST <l'utilisateur> » ; plus haut, seulement plus de questions.*

**R11. Remboursement sur un même compte** : même montant, le crédit dans les 30 jours après le débit, avec un mot rare
commun aux deux libellés. Exemple : −30 € Amazon, puis +30 € Amazon. Les mots trop fréquents sont calculés sur ton compte (plus de 5 % d'un côté, 3 fois au
moins), sans dictionnaire : ici, un mot ne pèse que par sa rareté chez toi. Ailleurs, une seule liste fixe de mots
pèse sur un type : celle des marchands des récurrents (R41).

**R12. Dépôt Bourse / Crypto / placement déjà validé** : même jour, même montant au centime ou un écart de frais (1 €
ou 2 % au plus, chacun le seul candidat de l'autre ce jour-là), depuis le même compte bancaire, sous le même libellé,
vers le même compte d'investissement qu'un dépôt auquel tu as répondu « oui ». Exemple : ton virement mensuel vers le
PEA. Comme une paire déjà validée ; la leçon
se tire de tout l'historique, avant comme après ta réponse (`transfer_patterns.contribution_shapes`). *Mesuré sur 53
mois, 62 dépôts contre 3 421 débits : le même jour, 51 dépôts prouvaient un seul débit, 4 plusieurs, 7 aucun ; à trois
jours, 50 et 6. Avec l'écart de frais : 57 prouvés au lieu de 54, 0,30 coïncidence par essai au lieu de 0,25 une fois
les dépôts décalés de deux semaines à trois mois ; 2 € ou 3 % ne prouvaient rien de plus, pour 0,40.*

**R13. Une paire reconnue n'est jamais une règle par nom** : elle ne classe pas les autres opérations qui portent un
des deux libellés. Une opération n'a qu'une paire, et un dépôt ne justifie qu'une seule opération. Les paires se
règlent globalement, les plus proches d'abord, puis aucune jambe n'est laissée qui aurait pu être appariée
(`flows._closest_complete_matching`). Une opération dont le Oui tient ne rejoint aucune paire que « Lier » ne lie
(`transfer_patterns.deposited`).

### Ce qui reçoit une question

**R14. Une question n'est posée que lorsque quelque chose est en face, ou qu'une sortie dépasse le seuil** choisi par
l'utilisateur (`flows._asks_flow`).

**R15. Paire possible** entre deux comptes : sens opposés, même montant, même devise, au plus 2 jours ouvrés d'écart,
qu'aucune règle R7 à R11 ne confirme. Y compris entre deux comptes courants, même avec le même libellé, le même jour et
le même montant. Une paire seulement proposée ne change aucun total : les deux opérations comptent chacune de leur
côté. *Mesuré : presque toutes les paires vues une fois étaient un tiers qui remboursait un achat, pas un virement.*

**R16. Dépôt possible** : même montant ou un écart de frais, à 2 jours ouvrés au plus, comme une paire. La question
est posée en Oui / Non sur chaque opération concernée (`services/banking/contribution_decisions.py`) :

- **Oui** lie l'opération à ce dépôt, des deux côtés, et la compte en investissement ; il vaut pour le libellé (R12).
  Un type choisi à la main sur cette opération s'efface : ce qu'elle est décide de comment elle compte. Elle ne rejoint
  plus aucune paire, proposée ou automatique : seul « Lier » peut encore l'apparier, et il efface le Oui, comme un
  type choisi à la main ensuite. Une seule décision par opération. L'opération d'en face libérée compte seule (une
  entrée pose sa question à partir de 100 €, R22).
- **Non** la sort du rapprochement : elle se lit comme toute autre opération, et le dépôt peut faire face à une autre
  opération.

*Mesuré sur le dump (31/10) : les 12 opérations trouvées ou validées à quelques jours de leur dépôt étaient toutes à
2 jours ouvrés au plus, dont deux retraits Trade Republic d'un vendredi passés le mardi, que « 3 jours » manquait ; 4
ou 5 jours de calendrier laissaient un paiement au restaurant couvrir un versement PERU.*

**R17. Dans « À trier », ces opérations forment leur propre groupe** par compte, libellé et compte d'investissement, à
part de la question de type du libellé (`flows._asking_groups`). La question s'affiche sur la plus récente d'entre
elles : Oui / Non pour celle-ci, ou « Choisir » pour répondre aux cochées, chacune avec son propre dépôt. Un libellé
qui change demande un Oui par libellé. Ces questions passent en tête d'« À trier », le reste suit par montant : un Oui
apprend la forme et range seules les opérations suivantes qui la suivent. *Mesuré sur le dump : les 17 « VIR Virement
depuis Compte courant » portées par un virement de 500 € sans rien en face deviennent 16 face au PEA et 1 question de
type ; même montant à trier. 7 « oui », un par forme, rangent 58 des 59 sorties face à un dépôt le même jour, la
dernière attend la réponse sur sa paire proposée, et chaque page mensuelle lit comme l'historique entier ; avant, la
leçon ne voyait que les mois chargés par la page : 45 sorties comptaient en dépense sur leur mois.*

**R18. Sur une opération classée par une règle, un récurrent ou à la main, la question se pose aussi**, mais seulement
quand un dépôt lui fait face **le même jour** : le dépôt arrive souvent après coup, par un import. Oui remplace le
classement, Non le garde. Pas si elle est déjà classée en Investissement. Le Oui donné à une autre opération du
libellé ne la range pas : sa question reste, et la liste du groupe montre son classement. Un autre jour, le dépôt en
face est listé (R26). *Vu sur le dump : deux opérations classées Non compté avant l'import Trade Republic (500 € le
15/04/2024, 423,35 € le 28/01/2026) ne posaient plus de question.*

**R19. Une réponse tient tant que les deux côtés se font encore face** : un dépôt supprimé ou déplacé depuis la laisse
sans effet. Elle reste alors dans l'historique des réponses, pour être retirée. Supprimer le compte d'investissement
(Bourse, crypto ou placement) supprime les réponses données face à ses versements et retraits, comme supprimer un
compte bancaire supprime les siennes. Ni un type choisi, ni une règle n'apprennent un dépôt : une règle parle d'un
libellé, et un libellé peut aller au PEA un jour et ailleurs le lendemain.

**R20. Si une paire et un dépôt sont tous deux possibles, la même question montre les deux**, une ligne chacun avec
son bouton (R34) : l'opération en face, « Celle-ci », ou le versement sur le compte d'investissement, « Celui-ci » (le
Oui, qui écarte la paire). Tant que la paire est proposée, le dépôt
n'est qu'affiché, même déjà validé pour ce libellé. *Mesuré sur le dump : un cas (−20 € du 05/02/2024), dont le dépôt
s'affichait jusque-là, à tort, sur une autre sortie de 20 € deux jours plus tard.*

**R21. Sortie sans rien en face, au-dessus du seuil** du curseur (500 € par défaut, réglable,
`UserSettings.bank_question_threshold`). Le seuil porte sur l'opération, pas sur le total du libellé. Les questions
restent regroupées par libellé, « celle-ci » par défaut. *Mesuré sur un historique de 4 ans : 6 opérations à 1 000 €,
20 à 500 €, 54 à 250 €, 139 à 100 €. À 100 €, on retrouve à peu près les questions d'avant le 09/10, cartes comprises :
sans dictionnaire, un achat carte et un virement ne se distinguent pas.*

**R22. Entrée sans rien en face** (« revenu ou remboursement ? »), groupée par signature de libellé, si le groupe
dépasse 100 € sur tout l'historique (`flows.FLOW_QUESTION_MIN_AMOUNT`). Seul l'utilisateur sait si les 50 € d'un ami
sont un remboursement. Un crédit pris dans un récurrent revenu seulement proposé pose sa question dans Récurrents, pas
ici (R43). *Mesuré sur 53 mois : 186 libellés demandés ; sous 100 € il y en avait 85, 2 393 € en tout, 1,4 % de ce que
les questions pesaient.*

**R23. Les questions sont rangées par année, les plus récentes d'abord** ; dans l'année, celles qui font face à un
dépôt en tête, puis par montant, puis les plus récentes (`flows.review_queue`).

**R24. Une opération sans aucun mot dans son libellé** (« 00160669 », « H&L ») **garde une identité : son texte
entier**, lettres et chiffres, casse et accents repliés, marqué d'un `#` qu'aucun mot ne porte
(`labels.label_signature`). Elle pose ses questions, se groupe et porte une règle comme une autre, mais seulement avec
les opérations du même texte exact, jamais avec un libellé proche (R10, R27) : une référence qui change à chaque fois
fait un groupe d'une seule opération. Sans lettre ni chiffre (libellé vide), l'opération reste sans identité : elle ne
pose aucune question, ne porte aucune règle, compte par défaut et se type à la main avec « celle-ci ».

Aucune liste de mots n'entre dans l'identité (R1, R40). Limite connue : un libellé fait des seuls mots du format de sa
banque (« CARTE 12/03/25 A.R.E.A. CB*0837 », lu « carte cb ») partage son identité avec les autres paiements de ce
format, et « les prochaines aussi » sur l'un atteindrait les autres : l'utilisateur peut alors séparer ce libellé
(R54). *Mesuré sur le dump (12/11) : 7 opérations sans
mot, dont 5 crédits « 00160669 » (301,50 €) qui posent désormais leur question ; « carte cb » couvre 16 opérations de
8 marchands sur un compte, « carte cb le » 3, « envoye paiement par » 1, aucune règle ne porte dessus.*

### Ce qui ne reçoit pas de question

**R25. Une sortie sans rien en face, sous le seuil, est une dépense** : si tous les comptes d'épargne et
d'investissement sont dans l'appli, elle ne peut être que ça. Le curseur sert à ça : en le bougeant, on voit combien
de sorties passeraient en question, et on attrape un compte oublié (3 000 € partis vers un PEA ouvert ailleurs). Autour :

- dans l'onglet Comptes, dès qu'il y a un compte, une phrase qu'on peut fermer (fermée pour de bon sur ce
  navigateur) : « Ajoutez aussi vos livrets et vos comptes de bourse : sinon, les virements vers eux compteront comme
  des dépenses. » ;
- à côté des totaux du Réel : « X € à trier (N opérations) », avec un lien vers « À trier ».

**R26. Les dépôts sans contrepartie sont listés**, sous les questions d'« À trier » : les dépôts (et les retraits)
déclarés sur un compte Bourse, Crypto ou un placement pour lesquels aucune opération bancaire n'a été trouvée. C'est
l'inverse du curseur : il attrape un compte d'investissement oublié, cette liste attrape un compte bancaire oublié. Ils
ne reçoivent pas de question et ne changent aucun total (`flows._unfaced`, `contributions.unfaced`).

- Un dépôt n'y figure que si rien ne le montre ailleurs : ni un Oui, ni une question posée sur une opération en face
  (une paire proposée la pose aussi, le dépôt à côté), ni une opération en face déjà comptée en investissement. « En
  face » se lit comme pour la question : sens opposé, même montant ou un écart de frais, à 2 jours ouvrés au plus (R5).
- Un « Non » le remet dans la liste. Une opération classée par une règle, un récurrent ou à la main ne pose sa
  question que le même jour : un autre jour, le dépôt en face est listé.
- Un pour un : une opération ne montre qu'un dépôt, le plus proche (le montant lui-même avant un écart de frais).
- Un PER ou une épargne salariale peut être marqué « alimenté hors de mes banques » (abondement de l'employeur,
  prélèvement sur salaire) : ses versements ne sont plus cherchés côté banque, ni proposés, ni listés ; ses rachats le
  sont toujours. Interrupteur par compte, coupé par défaut (`PlacementAccount.paid_in_outside_banks`).
- Seuls les dépôts compris dans l'historique bancaire sont jugés, et pas les 2 derniers jours ouvrés, où l'autre côté
  peut ne pas être encore passé.

*Mesuré sur le dump (31/10) : 110 dépôts déclarés ; avant, 2 étaient couverts sans un mot (13,69 € face à un
McDonald's de 14,10 € le lendemain ; 14,70 € sur le portefeuille crypto face à un achat carte Binance de 15 €, sans
doute le vrai). Ils deviennent des questions, avec les deux retraits Trade Republic : 5 opérations demandées, aucune
classée autrement, 0 dépôt couvert sans un mot, 21 listés (12 versements PERU de l'employeur, 7 sur le CTO, 2 sur le
portefeuille). Dépôts décalés de 2 semaines à 3 mois : 8,9 questions de hasard par essai au lieu de 5,6. Avec
l'interrupteur PER : 12 des 21 dépôts listés étaient des versements PERU de l'employeur.*

### Répondre

**R27. Trois portées** (`flows.set_transaction_type`, `TypeScope`) :

- **Celle-ci** : le choix par défaut.
- **Celles que je coche** : parmi les opérations du même nom, listées (« Choisir parmi les N opérations de ce
  libellé »). Liste ouverte, la réponse porte sur les opérations cochées, celle de la question cochée d'avance ;
  fermée, sur celle-ci. Chacune reçoit son propre type, comme si on avait répondu une par une : aucune règle, rien
  pour les prochaines. Seules des opérations du même compte et du même sens peuvent être cochées ensemble.
- **Les prochaines aussi** : case explicite, qui crée une règle (jamais sur un libellé séparé, R54). Les règles sont
  le dictionnaire de l'utilisateur : onglet « Règles » de Banque, chacune avec son libellé, son compte, son sens et le
  nombre d'opérations qu'elle type ; on y change son type ou on la supprime. Une règle parle des opérations à venir, elle n'a pas sa place dans
  l'historique des réponses. Une règle née d'une réponse atteint le libellé exact et les libellés proches (0,6 de mots
  en commun, les mots trop fréquents sur ce côté du compte mis de côté), sur ce compte et ce sens
  (`services/banking/type_rules.py`).

**R28. Une règle par nom n'empêche jamais une paire trouvée plus tard** : la paire passe avant (R37).

**R29. Une règle écrite à la main**, bouton « Nouvelle règle » de l'onglet « Règles » : le libellé « contient » ou
« commence par » un texte, sur un compte ou sur tous (ceux ajoutés plus tard compris), en entrées ou en sorties, et
le type. Jamais de regex : le texte est pris tel quel, casse, accents et espaces répétés repliés ; 3 caractères au
moins, 100 au plus. L'aperçu est obligatoire avant d'enregistrer : les opérations qu'elle comptera et combien changent
de type, les règles à qui elle en prend et celles, plus précises, qui gardent les leurs (nommées), ce qu'une paire ou
un dépôt garde, les récurrents confirmés qu'elle sortirait du fixe (R48), et un avertissement quand elle touche plus
de la moitié des opérations de ce côté. Toute règle, même née d'une réponse, peut être réécrite ainsi ; la même règle
écrite deux fois remplace la première (`flows.preview_rule`, `flows.write_rule`).

**R30. Un choix fait à la main l'emporte toujours sur une règle.** L'aperçu liste ceux d'un autre type que la règle
(les autres ne disent rien) ; une case, décochée par défaut, « Les passer aussi en … », retire ces choix et les donne à
la règle. Les paires ne sont jamais concernées.

**R31. Dans l'autre sens**, un choix à la main ou une règle née d'une réponse qui passe désormais avant une règle
écrite le dit dans son message (« Pour ce libellé, elle passe avant la règle Contient « amazon » »).

**R32. Entre deux règles** : celle née d'une réponse (ce libellé-là, et les libellés proches) passe avant une règle
écrite ; entre règles écrites, le texte le plus long, puis celle d'un seul compte, puis la plus récente
(`type_rules.TypeRule.outranks`).

### Délier, annuler

**R33. Une paire proposée se pose en deux boutons : « Ensemble / Pas ensemble ».** On ne sait pas si c'est un
virement entre ses comptes ou un tiers qui rembourse un achat sur l'autre compte : l'autre côté s'écrit sur sa
ligne, « <compte> · <jour> · <libellé> », sans flèche ni badge, et la ligne sous les boutons dit seulement ce que compte
« Ensemble » — « Ensemble : elles s'annulent, aucune
n'est comptée » (ou « comptées en épargne » face à un compte épargne non réglementé, `together_type`). « Pas
ensemble » se lit seul, et son effet reste au survol. Une fois liée, la paire reprend sa flèche « vers / depuis
<compte> ». La question se pose sur le débit, ou sur la jambe que d'autres opérations aussi proches se disputent.

**R34. Quand une autre opération est aussi proche que celle proposée**, d'un autre compte ou d'un autre libellé, et
encore libre, la question la montre aussi. Dès que la question hésite entre plusieurs mouvements (paire nommée,
opérations aussi proches, dépôt en face, R20), chacun a sa ligne « <compte> · <jour> · <libellé> » et son propre
bouton « Celle-ci » (« Celui-ci » pour le dépôt), puis un seul « Aucune », qui refuse chaque paire montrée et répond
Non au dépôt : un refus ne valant que pour sa paire (R35), « Pas ensemble » aurait seulement fait proposer la
suivante. Le compte ouvre chaque ligne, marqué d'une icône selon ce qu'est le compte (courant, épargne,
investissement), jamais selon ce que l'opération a l'air d'être : sans dictionnaire, une carte et un virement ne se
distinguent pas. Une opération proposée en plus n'arrive qu'avec le nom de son compte : sans compte unique à ce nom,
l'icône reste neutre. Elle se pose sur l'opération
qu'elles se disputent. Une paire de livret est alors seulement proposée au lieu d'être validée
seule (R8). En attendant la réponse, les deux opérations comptent chacune de leur côté, comme toute paire proposée.
*Mesuré sur le dump : 2 paires de livret avaient pris la mauvaise des deux (−250 € Livret A face au revenu de Mme
Dormia Laure le 28/04/2025, −750 € LDD le 30/12/2024) ; ce sont les 2 seules questions de plus.*

**R35. Un refus ne vaut que pour sa paire.** Il n'apprend rien sur les libellés.

**R36. Historique de toutes les actions**, chacune annulable ; les règles ont leur page
(`services/banking/history.py`).

- « Historique de mes réponses », en bas d'« À trier » : chaque décision encore en vigueur (paires liées, refusées,
  annulations ; réponses sur un dépôt ; types choisis sur une opération ; récurrents confirmés ou refusés), la plus
  récente d'abord, lue là où chaque décision est déjà rangée, pas dans un journal à part. « Annuler » supprime la
  décision, donc tout revient exactement comme avant ; une décision annulée quitte la liste.
- Un type choisi à la main garde sa date (`type_override_at`, vide pour ceux d'avant le 09/10).
- Quand une paire reconnue depuis passe avant un type choisi à la main, l'historique le dit sur ce choix. Une paire
  reconnue après coup ne remplace jamais en silence un type choisi à la main.

### Ordre de priorité d'un type

**R37.** Ajustement de solde > paire reconnue > choix sur cette opération > dépôt prouvé > règle (née d'une réponse,
puis écrite) > récurrent > défaut (entrée = revenu, sortie = dépense). C'est `cashflow_types.resolve_type`, et rien
d'autre ne type une opération.

**R38. Une paire avec exactement un compte d'épargne** (réglementé ou de type « Épargne ») **compte en Épargne** ;
entre deux comptes courants, ou entre deux épargnes, elle n'est pas comptée. L'épargne se lit sur la jambe hors du
compte d'épargne (son débit met de côté, son crédit reprend) ; une paire non comptée se lit sur son débit
(`cashflow_types.counted_leg`).

**R39. Un virement reconnu entre deux comptes sort aussi l'opération des récurrents** (un remboursement, lui, la garde
comme échéance annulée). Une règle, non : l'opération reste dans son récurrent, mais compte selon la règle (R48).

### Ce qui ne décide jamais

**R40. Le moyen de paiement** (« CARTE », « VIR », « PRLV »…) **n'est pas lu du tout**, ni affiché ni filtré : le
libellé le dit déjà, rien ne se décidait dessus, et son dictionnaire dépendait de la banque. Deux listes fixes, écrites
dans le code, servent à nommer et regrouper : les mots du format bancaire et les mots vides
(`label_groups.NOISE_WORDS`), et les motifs des formats bancaires, date de carte, « CB*0837 », référence
(`label_groups._NOISE`). Elles font les noms affichés (Explorer, récurrents, Réel, MCP), les groupes de l'Explorer et
les contreparties du Réel, avec leur montant par ligne ; jamais un type, une paire, une question ou un total. Une seule
exception : le marchand des récurrents les reprend (R41). Aucune n'entre dans l'identité d'un libellé (R24).

### Récurrents

**R41. Détection par marchand, rythme et montant** (`services/banking/recurrence.py`, `merchants.py`) : un prix qui
change, un nom qui change, une pause restent le même récurrent. Le marchand d'une opération se lit sur les mots de son
libellé moins une **liste fixe** (`merchants.NOISE`) : les mots de `label_groups.NOISE_WORDS` (R40), plus ceux du format
long de Boursorama (« prelev », « sct »…), les formes juridiques, les entités HTML et quelques mots vides. Deux libellés
sont le même marchand quand leurs mots, pesés par leur rareté chez l'utilisateur, se recouvrent ; un libellé sans autre
mot prend son texte nettoyé des formats bancaires (`label_groups._NOISE`). Ce marchand décide quelles opérations
forment une série, laquelle reprend après un renommage, et quels crédits en sont les remboursements : ceux du même
marchand, ou d'un marchand dont le premier mot est celui d'un des siens (`recurring_series._refunding`). Une fois la
série confirmée, ces crédits comptent en dépense remboursée (R37), dans « dont … qui reviennent » et dans les charges
fixes : **c'est la seule liste fixe qui pèse sur un type et un total.** Elle n'entre ni dans une paire ni dans
l'identité d'un libellé (R24), et elle est figée dans l'identité des décisions marquées à la main
(`recurring.py`). Les couches actuelles, mesurées sur des cas réels, sont gardées. Mesurer sur le dump avant de
changer quoi que ce soit, liste comprise. *Mesuré le 12/11 : retirée sans rien à la place, 332 opérations (19 944 €)
changeraient de type ou de source.*

**R42. Rien n'est compté seul** : toute série détectée est proposée dans l'onglet Récurrents, et ne compte qu'une fois
confirmée ; ses remboursements liés aussi. Détecter large : une fausse proposition ne coûte qu'un « non », un récurrent
raté coûte en silence. Un loyer ou de l'argent de poche détecté est acceptable. *Mesuré sur le dump : les 25 séries
restent, les 15 qui comptaient seules (« auto ») deviennent des propositions ; les charges fixes passent de 686,88 € à
624,56 € par mois (les 5 confirmées) et les revenus récurrents de 251,27 € à 0 € jusqu'à validation ; « À trier » ne
bouge pas.*

**R43. Pas de seuil dans les récurrents** : une opération d'une série proposée, dépense ou revenu, n'apparaît que dans
Récurrents, pas dans « À trier ». Refusée, ses opérations au-dessus du seuil (ou son groupe de crédits, R22) tombent
dans « À trier ». Le lecteur voit `type_source = recurring` sur ces opérations, proposition comprise : le type est
celui du défaut et aucun total ne bouge tant que rien n'est confirmé (limite d'affichage, notée le 02/11).

**R44. Un dépôt en face, le même jour, passe devant le récurrent** : l'opération pose sa question « est-ce celui-ci ? »
dans « À trier », comme sous une règle. Oui = comptée en investissement et sortie de la détection des récurrents (un
récurrent fait de ces seules opérations disparaît de l'onglet, même confirmé ; il reste dans l'historique, et revient
si le Oui est annulé). Non = elle reste dans son récurrent. Seul le Oui sort une opération des récurrents : ni une
règle ni un type choisi à la main. *Mesuré sur le dump : aucun cas, rien ne bouge.*

**R45. Aucun moyen de paiement** : carte, prélèvement ou virement, on ne les lit pas pour décider d'une série, et aucun
mot n'est appris. La ressemblance des libellés (`merchants.py`) reste telle quelle, avec sa liste fixe (R41) : un
abonnement garde la même forme de libellé. Une
seule règle pour toutes les dépenses (`recurrence.offered`) :

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

Les revenus ont leur lecture (`recurrence._income_offered`) : un payeur qui n'envoie presque rien d'autre, des montants
au centime ou des pas serrés, et sur un livret réglementé un crédit sans paire ne peut venir que de la banque.

**R46. Les intérêts ne passent pas par le mot « INTER »** : un crédit sans paire sur un livret réglementé (le type du
compte) ne peut venir que de la banque, une fois l'an. Un renommage (EDF, OVH) se reconnaît au même montant au centime,
sur le même compte, à l'échéance suivante. Les retraits ne sont pas écartés d'avance : sans régularité, ils ne forment
pas de série. Un paiement par virement prend ses remboursements comme les autres, une fois confirmé. *Mesuré sur le
dump du 25/09 : les mêmes 25 séries, EDF, Olness et les intérêts compris (sans dictionnaire, l'ancienne règle carte les
perdait : un montant rond devait tomber au jour près), aucune nouvelle ; « À trier » ne bouge pas. Un effet : le loyer
confirmé « Frederic Durand », payé par virement, prend comme remboursement le crédit de 345 € du 29/07/2026 venu du
même nom.*

**R47. Fusionner** fait un récurrent de deux qui se suivent : un contrat repris (Orange puis Bouygues), une série que
la détection a coupée. Celui depuis lequel on fusionne reste ; l'autre décision part, et ce que l'utilisateur y avait
mis (nom, nature, cadence) passe à celui qui reste quand il n'en a pas. Sa date de fin ne passe pas. Deux séries
**simultanées** ne se fusionnent pas utilement : la série fusionnée n'a qu'un montant par échéance, son total reste
juste mais son mensuel est faux. Pour les regrouper, la nature (`recurring.merge`).

**R48. Une règle sur un récurrent confirmé.** La règle passe avant le récurrent, opération par opération (R37) :
classées autrement qu'en dépense (ou en entrée pour un revenu), ses opérations ne comptent déjà plus dans « dont … qui
reviennent ». Quand la règle atteint sa **dernière échéance**, les suivantes suivront : le récurrent sort aussi de ce
qui est fixe, partout à la fois (total et nombre de l'onglet Récurrents, mensuel de sa catégorie, charges fixes et
échéances attendues du Réel, outil MCP). Il reste listé à sa place, avec « Hors du total », et son détail nomme la
règle (`StoredRecurring.retyped`). Retirer la règle le fait revenir. Un choix fait à la main sur une seule opération
ne le sort pas : il ne parle pas des suivantes. Avant d'enregistrer, l'aperçu d'une règle écrite nomme les récurrents
qu'elle sortirait, et le choix d'un type pour tout un libellé le dit quand l'opération fait partie d'un récurrent
confirmé. Le payé en tout et le mois par mois restent ce qui a été payé. Si ce n'était pas un récurrent, c'est « Ce
n'est pas récurrent » qu'il faut dire, pas une règle.

**R49. Catégories = natures** : la liste fixe (Logement, Énergie…), choisie par l'utilisateur, jamais devinée ; pas de
catégories libres (`services/banking/natures.py`). Une catégorie s'ouvre sur toute son histoire, terminés compris :
payé en tout, mensuel, mois par mois par récurrent. Chaque récurrent porte son total et son mois par mois
(`paid_total`, `paid_by_month`), comptés comme « payé sur 12 mois » : échéances, hors échéance et ajouts à la main,
remboursements non déduits.

**R50. Un récurrent confirmé sur un compte pas à jour reste fixe.** Le statut `stale` dit qu'un compte n'est connu que
jusqu'à un jour d'avant la prochaine échéance (3 jours sans synchronisation, ou la dernière opération d'un compte
importé) : un compte que personne n'a synchronisé n'arrête pas ses récurrents. Il compte donc partout où un récurrent
actif compte : total et nombre de l'onglet Récurrents, charges fixes et revenus récurrents du Réel, mensuel de sa
catégorie, outil MCP (`recurring.FIXED_STATUSES`). Seules les **échéances attendues du mois en cours** ne le listent
pas : une échéance a pu passer sans être vue. Décidé le 02/11 ; avant, le Réel le comptait pour 0 € tandis que
l'onglet le gardait.

### Invariants techniques

**R51. Une seule lecture par opération.** Toute vue — liste du mois, À trier, Réel, Explorer, récurrents, aperçu de
règle, historique, MCP — lit une opération par `flows._filed`, qui ne fait qu'appeler `cashflow_types.resolve_type`
avec ce qu'un lecteur a chargé. Aucun lecteur ne type une opération autrement. Les lecteurs chargent les mois qu'ils
montrent et un mois de marge de chaque côté, parce qu'un virement du 30 peut arriver le 2 (`flows._paired_movements`).

**R52. Tout ce qui se dérive de l'historique entier vit dans `bank_transfer_patterns`** (formes de paires, mots
fréquents, questions ouvertes, porteurs de question, récurrents, formes de dépôt validées, Oui en cours, couverture
des comptes), rebâti entièrement quand `transfer_patterns.source_digest` change, jamais mis à jour en place. Le digest
tient aux comptes lisibles, aux livrets réglementés, aux lignes bancaires (nombre, dernière mise à jour, dernière
création), aux décisions de paire, de dépôt et de récurrent, aux règles, aux séparations de libellé (R54), aux
lignes des comptes d'investissement et placements, aux placements « alimentés hors banques », au seuil, et à
`_VERSION`. **Toute nouvelle entrée de la lecture entre dans le digest, et `_VERSION` monte quand la dérivation
change** : sinon un lecteur sert un résultat périmé sans erreur. Une règle est remplacée plutôt que modifiée, pour la même raison.

**R53. Les mesures se font sur le dump de développement, jamais sur une intuition** (R4). Un changement de seuil, de
tolérance ou de couche de détection se justifie par ses chiffres avant et après, notés dans le journal.

### Séparer un libellé

**R54. Un libellé qui mélange des opérations sans rapport se sépare.** Quand une identité (R24) couvre plusieurs
natures — « carte cb » pour les paiements de plusieurs marchands — l'utilisateur la sépare : bouton « Séparer » de la
liste « Choisir parmi les N opérations de ce libellé », là où le mélange se voit, et de la fenêtre du type, seul
endroit où se propose la règle d'un libellé qui ne pose aucune question ; « Regrouper », dans l'onglet
« Règles », l'annule (« Délier » reste aux paires, R7, R33). C'est sa décision sur un compte, un sens et une identité de
libellé, rangée à part (`services/banking/label_separations.py`), lue à chaque lecture comme une règle, jamais écrite
sur les opérations : elle vaut pour celles importées plus tard, et se défait sans rien abîmer. Aucune liste de mots,
aucune lecture du format de la banque (R1, R40) : c'est l'utilisateur qui dit que ce libellé mélange. Sur un libellé
séparé :

- chaque opération pose sa question seule, comme une opération au libellé unique : une entrée si elle fait à elle seule
  100 € (R22), une sortie à ses conditions habituelles (R14, R21), face à un dépôt comme toujours ;
- seule la portée « Celle-ci » reste : ni « Celles que je coche », ni « Les prochaines aussi » (R27), dans « À trier »
  comme dans le choix du type ; la mention « Libellé séparé » les remplace. L'API refuse la portée libellé ;
- aucune règle née d'une réponse ne l'atteint, ni la sienne ni celle d'un libellé proche (0,6, R27). Sa propre règle
  est supprimée en séparant ; avant de confirmer, le message dit combien d'opérations elle comptait, et combien
  d'opérations du libellé la règle d'un libellé proche cesse de compter. Une règle écrite à la main (R29) s'applique
  toujours : c'est la parole de l'utilisateur ;
- un Oui à un dépôt ne vaut plus que pour son opération (R12, R16) : les formes déjà apprises pour ce libellé ne rangent
  plus seules, chaque opération face à un dépôt demande ;
- les paires ne bougent pas (R7 à R11, R15) : elles reposent sur les montants et les comptes.

L'onglet « Règles » liste chaque séparation avec son libellé, son compte, son sens, le nombre de ses opérations et
« Regrouper » ; comme une règle, elle n'est pas dans l'historique des réponses (R36). Un libellé sans mot (`#…`, R24) se
sépare comme un autre ; un libellé sans identité, non. Supprimer un compte bancaire supprime ses séparations ; supprimer
le compte utilisateur aussi, et l'export du compte les contient.

---

## Textes d'interface

| Où | Texte |
| --- | --- |
| Paire proposée | ligne « <compte> · <jour> · <libellé> », icône du type de compte ; boutons « Ensemble » / « Pas ensemble » ; ligne « Ensemble : elles s'annulent, aucune n'est comptée » ou « comptées en épargne » |
| Plusieurs mouvements possibles | sans badge ; une ligne « <compte> · <jour> · <libellé> » (ou « versement de <montant> ») et son bouton « Celle-ci » / « Celui-ci » ; « Aucune » |
| Paire liée | « vers / depuis <compte> » |
| Dépôt possible | badge « vers <compte> ? » le même jour pour le même montant ; sinon « Versement de 48,88 € sur PEA le 4 sept. ? » ; Oui / Non / Choisir |
| Portées d'une réponse | « Celle-ci », « Celles que je coche » (« Choisir parmi les N opérations de ce libellé »), « Les prochaines aussi » |
| Règle écrite | bouton « Nouvelle règle » ; « contient » / « commence par » ; case « Les passer aussi en … » |
| Priorité annoncée | « Pour ce libellé, elle passe avant la règle Contient « amazon » » |
| Onglet Comptes | « Ajoutez aussi vos livrets et vos comptes de bourse : sinon, les virements vers eux compteront comme des dépenses. » |
| Réel | « X € à trier (N opérations) » |
| À trier | « Sans contrepartie bancaire » ; « Historique de mes réponses » ; « Annuler » |
| Récurrents | « Hors du total » ; « Ce n'est pas récurrent » ; « alimenté hors de mes banques » (placements) |
| Curseur | « Dépenses à vérifier à partir de <montant> » |
| Libellé séparé | bouton « Séparer » dans la liste « Choisir parmi les N opérations de ce libellé » ; dans la fenêtre du type, « Ce libellé couvre des opérations sans rapport ? Séparer » ; fenêtre « Séparer ce libellé » ; « Libellé séparé : chaque opération pose sa question seule. » (Annuler) ; mention « Libellé séparé » ; « Regrouper » (onglet Règles) |
| Carte du compte, après la première synchronisation d'un compte rempli par import (docs/bank-ledger.md, L1 et L11) | encadré « N opérations importées remplacées par celles de votre banque à partir du JJ/MM/AAAA : M réponses sont à redonner dans À trier. » (sans la seconde moitié quand aucune réponse ne part) ; s'il reste des lignes importées plus anciennes, « N opérations importées remplacées par celles de votre banque à partir du JJ/MM/AAAA. Les K opérations d'avant restent celles de votre import : votre banque ne remonte pas plus loin. », la partie des réponses à redonner reprise avant la seconde phrase s'il y en a ; « Vos opérations importées donnent X, votre banque Y : la courbe a été relevée (abaissée) de Z. Votre import ne remonte sans doute pas à l'ouverture du compte, ou il lui manque des opérations. », ou, s'il en reste, « Vos opérations donnent X, votre banque Y : la courbe a été relevée (abaissée) de Z à partir du JJ/MM/AAAA. Avant cette date, elle reste celle de votre import, d'où une marche ce jour-là. » ; croix pour le masquer |

---

## Journal

Les décisions et mesures, dans l'ordre. Une règle citée (R…) est celle d'aujourd'hui qui en découle.

### 25 septembre 2026

- Dump du 25/09 : aucun type choisi à la main ni décision de paire, rien à mesurer pour R36.
- Récurrents sans moyen de paiement mesurés sur ce dump : les mêmes 25 séries, EDF, Olness et les intérêts compris,
  aucune nouvelle ; « À trier » ne bouge pas (R46).

### 8 octobre 2026

- Un refus ne vaut que pour sa paire (R35). Avant, un seul refus bloquait toutes les paires aux libellés semblables,
  livrets compris : 8 virements vers le Compte plaisir perdus. Poussé (6a53229).
- Session du 08/10 : « libellé identique automatique » et « dépôt rapproché même pour une carte » essayés, défaits,
  jamais poussés.
- Règle « carte face à un virement reçu » (dictionnaire) : retirée.

### 9 octobre 2026

- Règles R1 à R3 validées : aucun faux positif, jamais un mot du libellé.
- Réponse « celle-ci » par défaut, case « toutes celles de ce libellé, et les prochaines » (R27).
- Une sortie n'est demandée que lorsqu'un dépôt ou un retrait lui fait face, ou au-dessus du seuil (R14, R21).
  Mesuré sur 4 ans : 6 opérations à 1 000 €, 20 à 500 €, 54 à 250 €, 139 à 100 €.
- Historique des réponses validé : lu là où chaque décision est déjà rangée, pas dans un journal à part (R36). Les
  types choisis d'avant cette date n'ont pas de `type_override_at`.
- Dépôt du même jour : question, puis automatique pour le même libellé vers le même compte (R12, R16).

### 10 octobre 2026

- Règles écrites à la main, « contient » / « commence par », avec aperçu, sans option : décidé ; les mots appris sont
  abandonnés (R3, R29).
- Un choix fait à la main l'emporte toujours sur une règle : validé (R30).
- Une règle parle des opérations à venir, elle n'a pas sa place dans l'historique des réponses : validé (R27).
- Récurrents : rien n'est compté seul, toute série proposée (R42). Mesuré : 25 séries gardées, 15 « auto » deviennent
  des propositions ; charges fixes de 686,88 € à 624,56 € par mois, revenus récurrents de 251,27 € à 0 €.
- Aucun moyen de paiement dans les récurrents (R45).

### 12 octobre 2026

- Dépôt : Oui / Non stockés des deux côtés, leçon tirée de tout l'historique (R12, R16). Mesuré : 7 « oui » rangent 58
  des 59 sorties face à un dépôt le même jour ; avant, 45 sorties comptaient en dépense sur leur mois.
- Presque toutes les paires proposées du dump sont un tiers qui rembourse : le badge dit « avec <compte> ? » sans
  flèche (R33).
- Moyen de paiement retiré partout, affichage et filtre compris (R40).

### 13 octobre 2026

- Dans « À trier », les opérations face à un dépôt forment leur propre groupe, en tête (R17). Mesuré : 17 « VIR
  Virement depuis Compte courant » deviennent 16 face au PEA et 1 question de type.
- Les règles quittent Paramètres › Banque pour l'onglet « Règles » de Banque (R27).
- Un dépôt en face, le même jour, passe devant le récurrent (R44). Mesuré : aucun cas.

### 15 octobre 2026

- Sur une opération classée à la main, la question se pose aussi quand un dépôt lui fait face le même jour (R18). Vu
  sur le dump : 500 € le 15/04/2024 et 423,35 € le 28/01/2026, classées Non compté avant l'import Trade Republic.
- Une paire de livret contestée est seulement proposée (R8, R34). Mesuré : 2 paires avaient pris la mauvaise des deux
  (−250 € Livret A le 28/04/2025, −750 € LDD le 30/12/2024) ; 2 questions de plus.

### 18 octobre 2026

- Fusion : nom, nature et cadence gardés de part et d'autre ; corrigé, le nom revenait au libellé de la banque (R47).
  Poussé (4b45539).
- Catégories de récurrents par nature, terminés compris, total et mois par mois (R49). Poussé (6f9ac2d).

### 19 octobre 2026

- Une règle qui classe autrement un récurrent confirmé : hors du total, nommée, annoncée avant (R48).

### 31 octobre 2026

- Dépôt : 2 jours ouvrés au lieu de 3 jours de calendrier (R6, R16). Fait (6bb28c4). Mesuré : 12 opérations toutes à
  2 jours ouvrés au plus, deux retraits Trade Republic du vendredi passés le mardi.
- Une seule règle « en face » pour la question et l'alerte ; un Non remet dans l'alerte (R5, R26). Fait (fda3d82).
  Avant, la liste prenait pour en face un écart de frais un autre jour, et ignorait les « Non » : 110 dépôts déclarés,
  2 couverts sans un mot. Après : 5 demandées, 0 couverte, 21 listées ; 8,9 questions de hasard par essai au lieu de
  5,6 une fois les dépôts décalés.
- PER, épargne salariale : « alimenté hors de mes banques » (R26). Fait (69cd7db). 12 des 21 dépôts listés étaient
  des versements PERU de l'employeur.
- Un dépôt déclaré est validé, demandé ou listé (R5). Docs (6b64dfc).

### 2 novembre 2026

- Revue complète du modèle, docs restructurées en règles numérotées, textes d'interface et journal ; rien retiré.
- Un récurrent confirmé sur un compte `stale` reste dans les charges fixes du Réel, le mensuel de catégorie et l'outil
  MCP, comme dans l'onglet (R50). Vérifié avant : Netflix 39,99 € dans l'onglet, 0 € dans le Réel. Les échéances
  attendues du mois le laissent de côté.
- Notées comme limites : une opération sans mot dans son libellé ne pose pas de question (R24) ; `type_source` vaut
  `recurring` sur une simple proposition (R43) ; un crédit d'un récurrent revenu proposé pose sa question dans
  Récurrents (R22).
- Le digest des `transfer_patterns` et la lecture unique `_filed` écrits comme invariants (R51, R52).
- Côté solde : l'ajustement d'ouverture est reculé, jamais supprimé, par un import ou une saisie plus ancienne
  (docs/bank-ledger.md).

### 8 novembre 2026

- Paire proposée avec plusieurs mouvements possibles (R20, R34) : une ligne et un bouton par mouvement, puis « Aucune ».
  Avant, « Ensemble » répondait pour la ligne « En face » écrite plus haut, et « Pas ensemble » ne refusait que la
  paire nommée : la suivante revenait. Le compte ouvre chaque ligne, avec l'icône de son type ; le badge
  « avec … ? » disparaît, paire seule comprise (R33).
- La fenêtre « Lier » marque de la même icône le compte de l'opération et de chaque candidate.
  Changement du front seul ; « Aucune » envoie les refus et le Non existants.
  Sur le dump : 2 questions concernées (−20 € du 05/02/2024 face à un paiement et au PEA ; +100 € du 05/04/2026 face
  à deux paiements carte).
- Le badge de type d'une entrée se lit comme le choix de type : « Remboursement » et non « Dépense ».

### 9 novembre 2026

- Supprimer un compte Bourse, crypto ou placement supprime les réponses Oui / Non données face à ses versements et
  retraits (R19). Avant, elles restaient dans l'historique des réponses sans plus rien ranger, et une réimportation
  ne les retrouvait pas : elle écrit de nouveaux identifiants. Lecture des opérations inchangée par construction,
  puisque R19 laissait déjà ces réponses sans effet ; non mesuré sur le dump.

### 12 novembre 2026

- Un libellé sans mot garde une identité, son texte entier marqué `#` (R24). Avant, il n'en avait aucune : ni
  question, ni règle. Sur le dump, 7 opérations (« H&l » ×2, « 00160669 » ×5) ; les 5 crédits « 00160669 », des
  remboursements de retraits, comptaient 301,50 € en revenu sans question. Mesuré avant et après : formes de paires
  (65, 365 occurrences), jambes (364 répétées, 300 épargne, 50 remboursements, 62 proposées, 4 liées), types et totaux
  par source, récurrents : rien ne bouge. « À trier » passe de 69 à 70 opérations, de 28 888,63 € à 29 190,13 € : une
  question de plus, celle des 5 « 00160669 ». Les 2 « H&l » (22,80 €) restent sous le seuil.
- Écarté : lire l'identité sans les mots de `label_groups.NOISE_WORDS`, pour séparer les marchands que « carte cb »
  confond. Ce serait une liste de mots qui décide (R1, R40). Noté comme limite dans R24.
- La saisie à la main accepte tout libellé qui porte une lettre ou un chiffre (docs/bank-ledger.md, L10).
- Séparer un libellé (R54), validé par Emilien en huit points : chaque opération demande seule, seule « Celle-ci »
  reste, aucune règle née d'une réponse ne l'atteint (la sienne est supprimée, annoncée avant), un Oui ne vaut plus pour
  le libellé, les paires ne bougent pas, la séparation est listée dans l'onglet « Règles ». Rangée dans sa propre table
  (`bank_label_separations`) plutôt que parmi les règles : ce n'est pas un type, et chaque lecteur des règles
  (onglet, aperçu, export, changement de type) l'aurait prise pour une. Une entrée de plus dans le digest, `_VERSION`
  de 38 à 39.
- Mesuré sur le dump, sans aucune séparation, avant et après : formes de paires (65, 365 occurrences), jambes (364
  répétées, 300 épargne, 50 remboursements, 62 proposées, 4 liées), types et totaux par type, source et sens,
  récurrents (13 dépenses, 682,86 € par mois ; 9 revenus, 2 601,36 €), « À trier » (70 questions, 29 190,13 € : 33
  entrées, 6 sorties, 31 paires ; 9 dépôts sans contrepartie) : rien ne bouge.
- « carte cb » en sorties séparé, dans une transaction annulée : 16 opérations, toutes sur le compte principal,
  278,07 €, 14 textes de libellé différents ; aucune n'atteint le seuil ni ne fait face à un dépôt, aucune règle ne
  portait dessus : aucune question ni aucun type ne bouge. Plus tôt ce jour, R24 en notait 25 sur 2 comptes : le
  compte portait sur toutes les lignes stockées, dont 9 parmi les 273 lignes d'un compte qui n'est plus à
  l'utilisateur et qu'aucun lecteur ne lit. R24 corrigée : 16 opérations, un compte.
- « Séparer » est aussi offert dans la fenêtre du type : « carte cb » ne pose aucune question, donc n'a pas de liste,
  et c'est dans cette fenêtre que sa règle se propose.
- Pour voir l'effet sur un libellé qui demande, toujours dans une transaction annulée : « Paiement envoyé par MANGOPAY »
  (13 entrées, 540 €, une question) séparé donne 3 questions (125 €, 111 €, 100 €) ; les 10 autres, chacune sous
  100 €, 204 € en tout, restent comptées en revenu sans question. « À trier » passe de 70 à 72 questions, de
  29 190,13 € à 28 986,13 €.

### 13 novembre 2026

- Les listes de mots écrites dans le code décrites telles qu'elles agissent (R11, R40, R41, R45). Avant, la doc disait
  qu'aucune ne décidait : vrai pour les noms et les groupes, faux pour le marchand des récurrents, qui décide des
  remboursements d'une série. Doc seule, aucun comportement changé.
- Mesuré sur le dump (deux audits du 12/11, recomptés par les lecteurs, hors les 273 lignes d'un compte qui n'est plus
  à l'utilisateur) :
  - `_NOISE` retiré sans rien à la place : aucun chiffre, 3 576 lignes de l'Explorer renommées ;
  - `NOISE_WORDS` retiré des groupes : aucun total, 20 lignes du Réel changent de montant par contrepartie ;
  - `merchants.NOISE` retiré : 332 opérations (19 944 €) changent de type ou de source, dont 136 crédits (12 770 €)
    qui deviennent des remboursements ; parts récurrentes annuelles négatives (2025 : −15 462 €) ; « À trier » de 72 à
    51 questions.
- Essayé sans aucun mot écrit en dur : les débuts et fins de libellés répétés sur les comptes de l'utilisateur (un mot
  de tête suivi d'au moins 8 noms différents est du format bancaire), plus les mots fréquents calculés. Sur le dump,
  zéro écart sur tous les chiffres, mais 774 lignes renommées, et un seuil sans marge : à 9, « sarl » n'est plus
  reconnu et Olness se détache d'Arverne ; à 6, une échéance Pathé sort de sa série. Les noms et les marchands
  dépendraient aussi de tout l'historique, un import pouvant renommer des opérations anciennes. Non retenu pour
  l'instant : les listes restent, jugées par Emilien comme un sujet à rouvrir. Avant de rouvrir, mesurer la stabilité
  en rejouant l'historique mois par mois.

### État des points

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
| Fusion : nom, nature et cadence gardés de part et d'autre (18/10) | poussé (4b45539) |
| Catégories de récurrents par nature, terminés compris, total et mois par mois (18/10) | poussé (6f9ac2d) |
| Règle qui classe autrement un récurrent confirmé : hors du total, nommée, annoncée avant (19/10) | fait |
| Dépôt : 2 jours ouvrés au lieu de 3 jours de calendrier (31/10) | fait (6bb28c4) |
| Dépôt : une seule règle « en face » pour la question et l'alerte, un Non remet dans l'alerte (31/10) | fait (fda3d82) |
| PER, épargne salariale : « alimenté hors de mes banques », versements plus cherchés, rachats si (31/10) | fait (69cd7db) |
| Récurrent `stale` compté dans le Réel comme dans l'onglet (02/11) | fait |
| Ajustement d'ouverture reculé par un import ou une saisie plus ancienne (02/11) | fait |
| Réponses de dépôt supprimées avec leur compte d'investissement (09/11) | fait |
| Libellé sans mot : identité `#` sur le texte entier, correspondance exacte (12/11) | fait |
| Séparer un libellé : chaque opération demande seule, aucune règle née d'une réponse ne l'atteint (12/11) | fait |
| Listes de mots écrites telles qu'elles agissent ; essai sans dictionnaire mesuré, non retenu (13/11) | fait |
