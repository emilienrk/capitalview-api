# Connexion bancaire Enable Banking — conception

Date : 2026-08-17
Portée : `capitalview-api` + `capitalview-web`
Rapport d'étude préalable : spike réalisé le 2026-08-16 contre Boursorama (2 comptes réels, API validée de bout en bout).

## Problème

CapitalView ne connaît le solde d'un compte bancaire que par saisie manuelle, puis l'extrapole :
`_apply_pending_cashflows` (`services/bank.py:140`) projette les occurrences de flux échues sur le
solde stocké à chaque appel de `get_user_bank_accounts`. Le solde affiché est donc une estimation qui
dérive jusqu'à la prochaine correction manuelle. Le domaine bancaire ne stocke aucune transaction :
`AccountHistory` ne porte qu'un instantané de valeur par jour, et `Cashflow` modélise des flux
*récurrents prévus*, pas des mouvements constatés.

**L'objectif est qu'à partir de la connexion, la courbe de solde soit exacte et vérifiable
indéfiniment.** L'historique rétrospectif est un amorçage, dont l'ampleur reste à déterminer — voir
l'encadré ci-dessous.

> ### Profondeur d'historique : question rouverte
>
> Nos appels API en mode restreint plafonnaient à **90 jours**, refus explicite à l'appui
> (« You can not request transactions more than 90 days in the past »). Mais l'export réalisé depuis
> l'interface de démonstration d'Enable Banking, sur les **mêmes comptes Boursorama**, contient
> **2 776 transactions remontant à octobre 2022 — près de quatre ans.**
>
> **La donnée existe donc bien côté banque ; c'est notre chemin d'accès qui la limitait.** Deux
> explications plausibles, non départagées : soit la fenêtre élargie documentée dans l'heure suivant
> l'autorisation, que nos mesures ont manquée ; soit un plafond appliqué aux applications en mode
> restreint, dont l'interface de démonstration est exemptée.
>
> **Expérience à mener avant de figer l'amorçage** : réautoriser, puis appeler immédiatement la
> stratégie exhaustive. Si quatre ans reviennent, l'amorçage devient un vrai import d'historique et
> non un simple démarrage — ce qui change la valeur perçue de la fonctionnalité et justifie de
> soigner le chemin « premier import ».

## Contraintes structurelles

**Architecture zero-knowledge.** La Master Key est dérivée du mot de passe, transmise par cookie
(`routes/auth.py:215`) et jamais stockée. Rien n'est déchiffrable hors session active. **Aucune
synchronisation en tâche de fond n'est donc possible** : le planificateur APScheduler (`main.py`)
tourne sans Master Key. Il ne peut agir que sur des colonnes en clair.

**Palier gratuit Enable Banking.** Le mode « restricted production » n'expose que les comptes liés
par le titulaire du compte Enable Banking lui-même. Une application ne peut donc pas servir plusieurs
personnes : **chaque utilisateur apporte sa propre application et sa propre clé privée**, sur le
modèle des clés IA (`models/user.py:67`, `services/settings.py:258`).

**Périmètre d'usage.** Emilien et quelques proches. Pas d'inscription publique.

## Décisions

Arbitrées le 2026-08-16/17. Elles font foi.

| # | Décision |
| --- | --- |
| 1 | Les transactions vont dans une **table dédiée**, jamais dans `Cashflow` |
| 2 | La clé privée vit **côté serveur, chiffrée avec la Master Key** |
| 3 | Déduplication croisée carte / compte courant **automatique** |
| 4 | Sélecteur de banque : composant fourni par Enable Banking ; les deux autres composants sont écartés |
| 5 | Le **solde comptable** fait foi, pas le solde temps réel |
| 6 | Courbe **ancrée et vérifiée** à partir de la connexion + amorçage rétrospectif marqué comme estimé |
| 7 | Callback sur l'**API**, pas sur le front |
| 8 | Sur la fenêtre d'amorçage, la donnée bancaire **écrase** la saisie manuelle |
| 9 | Synchronisation **automatique, plafonnée à une par jour**, plus un déclenchement manuel |
| 10 | **Tout est chiffré**, y compris les dates ; les requêtes passent par des **index aveugles** |
| 11 | Le rapport prévu / réalisé est **hors périmètre** de cette livraison |

## A. Modèle de données

Cinq tables nouvelles. Convention respectée : classe au singulier, table au pluriel, champs sensibles
suffixés `_enc`, index aveugles suffixés `_bidx`, horodatages techniques en clair.

### A1. `user_bank_connections` — les identifiants Enable Banking

Une ligne par utilisateur. Calque direct de `UserAIProvider`.

| Colonne | Type | Rôle |
| --- | --- | --- |
| `id` | int PK | |
| `user_uuid_bidx` | TEXT unique index | `hash_index(user_uuid, master_key)` |
| `application_id_enc` | TEXT | Identifiant d'application Enable Banking |
| `private_key_enc` | TEXT | Clé privée PEM, ~1,7 ko |
| `created_at` / `updated_at` | timestamptz | |

**La clé n'est jamais renvoyée au client.** L'API expose un booléen de présence, comme
`AIProviderConfig.has_key`.

### A2. `bank_authorizations` — les parcours d'autorisation en cours

Ligne éphémère, créée à l'ouverture du parcours, consommée au retour.

| Colonne | Type | Rôle |
| --- | --- | --- |
| `id` | int PK | |
| `user_uuid_bidx` | TEXT index | |
| `state_bidx` | TEXT unique index | `hash_index(state, master_key)` — permet de retrouver la ligne au retour sans stocker `state` en clair |
| `aspsp_name_enc`, `aspsp_country_enc` | TEXT | Banque visée |
| `authorization_id_enc` | TEXT | Identifiant d'autorisation, distinct de l'identifiant de session |
| `created_at` | timestamptz | |
| `expires_at` | timestamptz | Purge des parcours abandonnés |

### A3. `bank_sessions` — les consentements actifs

Une ligne par consentement bancaire. Un utilisateur peut en avoir plusieurs (une par banque).

| Colonne | Type | Rôle |
| --- | --- | --- |
| `uuid` | TEXT PK | |
| `user_uuid_bidx` | TEXT index | |
| `session_id_enc` | TEXT | Identifiant de session Enable Banking |
| `aspsp_name_enc`, `aspsp_country_enc` | TEXT | |
| `status` | TEXT **en clair** | Dernier état connu parmi les huit |
| `consent_valid_until` | timestamptz **en clair** | Permet au job nocturne de notifier sans Master Key |
| `authorized_at` | timestamptz | |
| `created_at` / `updated_at` | timestamptz | |

`status` et `consent_valid_until` sont en clair **délibérément** : ce sont des métadonnées
opérationnelles, non sensibles, et c'est la seule façon qu'un job de fond puisse produire une
notification d'expiration (cf. `models/notification.py`, déjà en clair par le même raisonnement).

### A4. `bank_account_links` — le rattachement compte CapitalView ↔ compte bancaire

| Colonne | Type | Rôle |
| --- | --- | --- |
| `uuid` | TEXT PK | |
| `user_uuid_bidx` | TEXT index | |
| `bank_account_uuid_bidx` | TEXT unique index | `hash_index(bank_account.uuid, master_key)` |
| `session_uuid` | TEXT FK → `bank_sessions.uuid` | |
| `identification_hash_bidx` | TEXT index | `hash_index(identification_hash, master_key)` — **clé de rattachement durable** |
| `account_uid_enc` | TEXT | Identifiant technique, **valable seulement tant que la session est autorisée** |
| `anchor_date` | DATE **en clair** | Date du dernier relevé de solde réel |
| `anchor_balance_enc` | TEXT | Solde comptable à cette date |
| `last_synced_at` | DATE **en clair** | Plafonnement de la synchro à une par jour |
| `last_reconciliation_gap_enc` | TEXT nullable | Écart constaté au dernier contrôle ; `NULL` = pas d'écart |
| `created_at` / `updated_at` | timestamptz | |

**Point critique** : `account_uid_enc` est jetable. Il change à chaque nouvelle session. Seul
`identification_hash_bidx` permet de retrouver le compte après une reconnexion.

### A5. `bank_transactions` — les mouvements constatés

Calque de `StockTransaction` / `CryptoTransaction` pour les conventions, enrichi des index aveugles
qu'impose le volume.

| Colonne | Type | Rôle |
| --- | --- | --- |
| `uuid` | TEXT PK | |
| `account_id_bidx` | TEXT index | `hash_index(bank_account.uuid, master_key)`, comme stock/crypto |
| `period_bidx` | TEXT index | `hash_index("YYYY-MM" de la date retenue, master_key)` |
| `entry_ref_bidx` | TEXT nullable index | `hash_index(entry_reference, master_key)` — déduplication intra-compte |
| `dedup_bidx` | TEXT index | `hash_index("date|montant|sens", master_key)` — déduplication croisée carte / compte courant |
| `amount_enc`, `currency_enc` | TEXT | Montant décimal en chaîne, devise d'origine |
| `credit_debit_enc` | TEXT | Sens |
| `status_enc` | TEXT | Un des sept statuts |
| `booking_date_enc`, `value_date_enc`, `transaction_date_enc` | TEXT nullable | Les trois dates, telles que fournies |
| `remittance_enc` | TEXT nullable | Libellé, lignes concaténées |
| `created_at` / `updated_at` | timestamptz | |

**Index unique composite** sur `(account_id_bidx, entry_ref_bidx)` pour rendre la déduplication
intra-compte infalsifiable au niveau base. Nullable, donc les transactions sans référence y échappent
— elles retombent sur `dedup_bidx`.

**Pourquoi des index aveugles et pas des dates en clair.** Un index aveugle ne supporte que
l'égalité : on énumère les valeurs cherchées et on interroge par `IN`. Au mois, une année représente
douze valeurs, un intervalle entre deux synchros une ou deux. Le tri fin se fait en mémoire sur un
mois, jamais sur tout le compte — ce qui évite exactement le piège de performance de
`get_all_user_cashflows`, qui déchiffre toute sa table à chaque appel.

## B. Client Enable Banking

Nouveau module `services/banking/`, **séparé du framework d'import**. Le contrat `ImportParser`
(`services/imports/base.py`) est entièrement centré CSV — `detect(csv_content)`,
`preview(session, csv_content, …)` — et l'élargir contaminerait les huit parseurs existants.

### B1. Authentification

Jeton JWT signé RS256, en-tête `{typ, alg, kid: application_id}`, corps
`{iss: "enablebanking.com", aud: "api.enablebanking.com", iat, exp}`. **Durée de vie plafonnée à
86 400 secondes** par l'API ; on prend une heure. Base : `https://api.enablebanking.com`.

### B2. En-têtes de contexte utilisateur

`GET /aspsps` renvoie, **par banque**, la liste des en-têtes exigés. Règle **tout ou rien** : les
fournir tous ou aucun, sinon erreur dédiée. Ils sont renseignés depuis la requête réelle de
l'utilisateur (adresse IP, agent) et **jamais fabriqués**. Comme la synchro tourne toujours en
session active, l'information est disponible — et elle exonère des limites de récupération en
arrière-plan.

### B3. Pagination

Trois règles, toutes vérifiées sur le terrain :

1. **Une page vide n'est pas la fin.** Le premier appel a renvoyé zéro transaction *et* une clé de
   continuation ; le second, 297. On s'arrête sur l'absence de clé, jamais sur une liste vide.
2. **La clé accompagne les paramètres d'origine.** L'envoyer seule produit `WRONG_CONTINUATION_KEY`.
3. **La clé n'est valable que dans la session courante.** Elle n'est ni persistable ni reprenable ;
   une récupération interrompue repart du début.

Le nombre de pages est borné pour éviter une boucle sur une clé qui se répéterait.

### B4. Stratégies de récupération

> ### La recette du premier import : `longest` **ET** un `date_from` ancien
>
> Mesuré contre la banque fictive chargée de quatre ans de données réelles :
>
> | Appel | Transactions | Période |
> | --- | --- | --- |
> | `strategy=longest`, sans `date_from` | 1 987 | 2 ans |
> | `strategy=default` + `date_from=2022-01-01` | **2 776** | **3 ans 10 mois** |
> | `strategy=longest` + `date_from=2022-01-01` | **2 776** | **3 ans 10 mois** |
>
> **`longest` seul s'auto-limite.** Malgré son nom, il ne remonte pas au plus ancien disponible :
> sans borne basse explicite, il s'arrête à deux ans. Le combiner avec un `date_from` volontairement
> très ancien est ce qui débloque la totalité de l'historique.
>
> Le premier import doit donc passer `strategy=longest` **et** un `date_from` largement antérieur à
> toute donnée plausible. Omettre le second, c'est perdre silencieusement des années sans aucune
> erreur — le cas le plus insidieux du chantier.

`strategy=default` ensuite, sur une fenêtre courte partant de l'ancre : la stratégie exhaustive coûte
des appels supplémentaires à la banque et augmente le risque de quota.

Sur `WRONG_TRANSACTIONS_PERIOD`, l'erreur renvoie **la date la plus ancienne autorisée** : on
recadre dessus au lieu de deviner. C'est la réaction attendue quand la banque bride réellement la
profondeur, comme Boursorama le fait en production restreinte.

### B5. Erreurs

**La logique se branche sur le code d'erreur métier, jamais sur le statut HTTP** — l'expiration de
session remonte en 401, mais 401 recouvre d'autres causes. Familles et réactions :

| Famille | Réaction |
| --- | --- |
| Session expirée / révoquée / fermée / annulée / inexistante | Marquer le lien à reconnecter, préserver le rattachement |
| Code d'autorisation expiré ou invalide, déjà autorisé | Relancer le parcours ; l'idempotence couvre le rejeu |
| URL de redirection non autorisée | Message de configuration explicite |
| En-tête utilisateur absent ou invalide | Corriger l'envoi ; règle tout-ou-rien |
| Période invalide | Recadrer sur la borne renvoyée |
| Clé de pagination invalide | Reprendre du début |
| Quota banque dépassé | Attendre six heures |
| Erreur ou indisponibilité banque | Reprise progressive : 1 min, 1 h, 2 h, 4 h |
| Mauvaise banque fournie | Relire le catalogue : la banque a pu être renommée |

## C. Parcours de liaison

### C1. Contrôle de configuration préalable

Dès que l'utilisateur dépose ses identifiants, un appel à `GET /application` vérifie **en un coup**
que la clé est valide, que l'application est active, et que l'URL de callback de CapitalView figure
bien dans les URL de redirection déclarées. Le diagnostic est rendu **avant** que l'utilisateur ne
parte s'authentifier chez sa banque. C'est le meilleur rapport valeur/effort du parcours.

### C2. Ouverture

`POST /auth` avec `psu_type: "personal"`, l'URL de callback, un `state` aléatoire, et
**`valid_until` fixé au maximum autorisé par la banque**.

> Sémantique contre-intuitive et documentée : la durée demandée est ajustée à la hausse si elle est
> sous le minimum de la banque, **mais la session expire strictement à la valeur demandée, même si le
> consentement reste valide côté banque**. Demander dix jours par confort imposerait une
> ré-authentification forte tous les dix jours là où la banque en autorisait cent quatre-vingts. On
> lit donc `maximum_consent_validity` dans le catalogue et on demande le maximum.

### C3. Retour

Le retour porte `code` et `state`, **ou** une erreur (`access_denied` sur refus ou annulation). Trois
issues à traiter, pas deux.

**L'URL de callback ne peut contenir aucun paramètre de requête** — le portail rejette l'ajout d'une
URL en comportant, avec un message explicite. Le chemin doit donc être fixe, et **aucune information
contextuelle ne peut transiter par la chaîne de requête de `redirect_url`** : tout doit passer par le
`state`, qu'Enable Banking rattache lui-même à la redirection. C'est une contrainte à respecter dans
le choix du chemin de la route et dans toute idée de callback paramétré (par utilisateur, par compte,
par environnement).

`state` est validé par recherche de `hash_index(state, master_key)` dans `bank_authorizations`.
**La documentation ne mentionne nulle part son rôle : c'est une exigence de sécurité maison**, sans
laquelle la route est ouverte à l'injection d'une connexion tierce.

Le cookie de Master Key est en `SameSite=Lax` : il accompagne une navigation de haut niveau en GET,
donc le retour direct sur l'API fonctionne.

**Cas du retour sans session.** Près de la moitié des grands réseaux français basculent vers
l'application bancaire mobile, donc le retour peut atterrir dans un navigateur sans session
CapitalView. Vu le périmètre d'usage retenu, on se contente d'une page expliquant de terminer dans
l'onglet connecté. **La mécanique de report du code chiffré côté serveur n'est pas implémentée** tant
que le besoin n'est pas avéré.

### C4. Réponse d'ouverture de session

`POST /sessions` renvoie l'identifiant de session et les comptes. **Certaines informations ne sont
fournies qu'une seule fois** — la documentation le signale sans préciser lesquelles. On persiste donc
l'intégralité de ce qui est utile à la réception, sans compter sur une relecture ultérieure.

## D. Synchronisation

### D1. Déclenchement

**Automatique, une fois par jour, plus un bouton manuel.**

**Elle ne doit surtout pas être branchée dans `get_user_bank_accounts`** comme l'est
`_apply_pending_cashflows` : la page Banque attendrait alors un appel réseau vers la banque à chaque
chargement. Le front lit `last_synced_at` dans la charge utile des comptes et déclenche
`POST /banking/sync` **après le rendu**. Le plafond quotidien est **revérifié côté serveur** —
le front n'est pas une autorité.

### D2. Séquence, par compte lié

1. `GET /accounts/{uid}/balances` → retenir le **solde comptable**.
2. `GET /accounts/{uid}/transactions` depuis `anchor_date`, paginé.
3. Déduplication (§E), insertion des nouvelles lignes.
4. **Contrôle de réconciliation** (§D3).
5. Nouvelle ancre : solde et date du jour.
6. Réécriture des instantanés `AccountHistory` de l'ancienne ancre à **hier**.

### D3. Contrôle de réconciliation

> `ancre précédente + somme des mouvements comptabilisés de la période = ancre actuelle`

Si l'égalité tombe juste, la courbe de la période est **exacte** et peut être présentée comme telle.
Sinon l'écart est stocké et daté : il signale un mouvement manquant ou compté deux fois. C'est le
détecteur du double comptage carte / compte courant, et du repli de déduplication quand la référence
est absente.

**Condition de validité** : comparer des grandeurs comparables. Solde comptable avec mouvements
comptabilisés uniquement. Les opérations en attente sont exclues du calcul et servent seulement à
l'affichage du solde du jour — sur les données du spike, c'est exactement l'écart de 162,63 € entre
les deux soldes publiés.

### D4. Écriture de la courbe

`import_bank_account_history` (`services/bank.py:338`) remplit déjà jusqu'à **hier**, jamais
aujourd'hui. **C'est la convention voulue** : elle laisse aux opérations en attente le temps de se
comptabiliser. On s'y conforme sans la modifier.

> ### ⚠️ Piège destructeur
>
> Le mode `overwrite=True` de cette fonction **supprime la totalité de l'historique du compte**, pas
> seulement la fenêtre visée : il appelle `delete_bank_account_history` sans borne. L'utiliser pour
> la décision 8 effacerait des années de saisie manuelle. Son autre mode, à l'inverse
> (`on_conflict_do_nothing`), n'écraserait rien du tout.
>
> **Aucun des deux modes existants ne convient.** Il faut un remplacement **borné à une fenêtre de
> dates** : ne remplacer que les instantanés compris dans l'intervalle traité, et ne toucher à rien
> en dehors. Nouveau chemin de code, testé explicitement sur un compte possédant de l'historique
> antérieur.

### D5. Neutralisation de la projection

Un compte lié reçoit un solde réel : la projection des flux devient fausse, puisqu'elle ajouterait un
salaire déjà contenu dans le solde. **Le compte est donc exclu de `_apply_pending_cashflows`** — tout
en **avançant `balance_updated_at`**, sinon une déconnexion ultérieure déclencherait un rattrapage de
plusieurs mois d'un coup, exactement le piège documenté dans le plan du 2026-07-29.

Les flux prévus ne perdent pas leur rôle : ils cessent de muter le solde constaté, et continuent
d'alimenter les projections futures.

## E. Déduplication

Trois niveaux, du plus fiable au plus approximatif.

1. **Intra-compte, par référence.** `entry_reference` est « unique et immuable » et « utilisable pour
   faire correspondre des transactions entre plusieurs sessions » : la déduplication **survit à une
   reconnexion**. Mais il « n'est pas globalement unique » — d'où la clé composite
   `(account_id_bidx, entry_ref_bidx)`, jamais la référence seule.
2. **Repli quand la référence est absente.** Elle est facultative au contrat : seuls le montant, le
   sens et le statut sont obligatoires. Boursorama la fournit toujours, une autre banque peut ne pas
   le faire. On retombe alors sur `dedup_bidx`.
3. **Croisée carte / compte courant.** C'est le cas dominant, pas un cas marginal. Mesuré sur
   l'export réel de quatre ans : **1 360 des 1 464 opérations du compte carte, soit 93 %, existent
   aussi sur le compte courant** — et **aucune** ne partage sa référence. Le seul signal fiable est
   le triplet (date, montant, sens), d'où `dedup_bidx`. La détection est **scopée à l'utilisateur**,
   pas au compte. Sans ce niveau, l'immense majorité des dépenses carte serait comptée deux fois.

Cas particulier : les références non numériques (préfixées) sont des empreintes de contenu et sont
**identiques** sur les deux comptes — le niveau 1 les attrape déjà.

**Les opérations en attente ne sont jamais définitives.** Elles peuvent disparaître, changer de
montant, et **changer de référence** en passant au statut comptabilisé. Le modèle doit pouvoir les
corriger, pas seulement empiler des lignes. Deux des sept statuts — annulé et rejeté — invalident une
transaction déjà ingérée.

## F. Interprétation des données

Constats du spike, à respecter à la lettre :

- **La devise du compte est inexploitable** : les deux comptes renvoient le code ISO signifiant
  « pas de devise », sur `/details` comme dans la session. **La devise se lit sur l'objet de solde.**
- **Deux soldes coexistent**, comptable et temps réel. Prendre le premier élément de la liste
  donnerait un résultat faux une fois sur deux.
- **Le solde après opération n'est jamais fourni**, ni le taux de change, ni le code marchand, ni le
  code d'opération normalisé, ni les contreparties. **Aucune catégorisation par code marchand n'est
  possible** : la seule matière est le libellé libre.
- **`booking_date` est parfois absent** (2 cas sur 297). Repli sur les autres dates.
- **Une devise étrangère arrive sans taux de change.** Marquer l'opération comme non convertie plutôt
  que l'additionner naïvement.
- **L'ordre des transactions n'est pas garanti** — la documentation l'indique explicitement. Tout
  traitement supposant un flux trié est un bug en attente.
- **Ne jamais utiliser `transaction_id`** pour identifier une transaction : le contrat l'interdit,
  la valeur change d'un appel à l'autre.
- Les montants sont des chaînes décimales : à manipuler en décimal exact, jamais en flottant.

> **Piège dans la spécification OpenAPI elle-même** : les descriptions d'énumérations sont
> désalignées de leurs valeurs. L'état « autorisée » y porte le libellé « a expiré », l'état
> « invalide » porte « est autorisée », et les trois approches d'authentification sont décrites les
> unes pour les autres. **Mapper les états par leur nom, jamais par la description positionnelle.**
> Un générateur de code produirait ici une logique fausse sans erreur visible.

## G. Frontend

- **`SettingsBanking.vue`**, calqué sur `SettingsAI.vue`, avec deux différences : dépôt de **fichier**
  pour la clé privée (~1,7 ko, un champ texte ne convient pas) et affichage **copiable** de l'URL de
  callback à déclarer.
- **Sélecteur de banque** : composant web fourni par Enable Banking. Il ne consomme aucune identité
  d'application et **n'exige donc aucune déclaration d'origine** — contrairement aux deux autres
  composants, écartés pour cette raison. Il est traduit en français et sait masquer les intégrations
  en bêta.
- **Parcours de liaison guidé**, qui annonce la double authentification : lier un compte au portail
  **n'autorise pas** l'accès aux données, « y compris lorsqu'il s'agit du même compte ». Sans cet
  avertissement, l'utilisateur croira à un bug.
- **Page Banque** : état de chaque connexion, date de dernier sync, bouton de synchro manuelle,
  et signalement visible d'un écart de réconciliation.
- **Trois réseaux imposent de choisir une caisse régionale** — Crédit Agricole, Banque Populaire,
  Caisse d'Épargne. Le sélecteur doit gérer cette étape intermédiaire.

## H. Tests

**La banque fictive du bac à sable Enable Banking rend le parcours testable sans authentification
forte réelle** — sans elle, chaque validation exigerait un SCA sur téléphone. Elle ne demande aucun
identifiant et renvoie les transactions **par lots de dix**, ce qui exerce naturellement la
pagination.

Elle est déjà alimentée avec **les données réelles exportées des deux comptes Boursorama** :
2 776 transactions sur le compte courant depuis octobre 2022, 1 464 sur le compte carte depuis
mai 2024, les deux soldes, et les mêmes empreintes d'identification que la production. C'est un jeu
d'essai d'une qualité rare : volume réaliste, doublons croisés massifs, libellés authentiques.

> ### Ce que la banque fictive ne reproduit pas
>
> L'export ne contient **que des opérations comptabilisées, toutes en euros, toutes datées**. Trois
> cas limites du §F y sont donc **inatteignables** et doivent être couverts par des doubles :
>
> - **opérations en attente** — aucune dans l'export, alors qu'elles sont au cœur du décalage entre
>   les deux soldes et du passage de statut avec changement de référence ;
> - **devise étrangère sans taux de change** ;
> - **date de comptabilisation absente**.
>
> À noter aussi : dans l'export, `transaction_date` est **systématiquement nul**, alors qu'il était
> renseigné dans nos appels API directs. **La disponibilité des champs varie selon le chemin d'accès**
> — raison de plus pour que la logique de repli entre les trois dates soit testée explicitement.
>
> Enfin, **la banque fictive ne produit jamais de page vide** : la pagination y est régulière du
> premier au dernier appel. Le cas « page vide suivie d'une page pleine », observé en production
> chez Boursorama, doit donc être couvert par un double — c'est précisément le piège qui ferait
> conclure à tort qu'un compte n'a aucun historique.

Deux constats de terrain contredisent la documentation et méritent d'être connus :

- **Les lots font 100 transactions, pas 10.** La documentation annonce dix ; la mesure donne cent,
  soit 28 pages pour 2 776 transactions. Le code ne doit dépendre d'aucune taille de lot.
- **Le filtrage par dates fonctionne** avec la banque fictive, contrairement à ce qu'annonce la
  documentation sur les bacs à sable. C'est ce qui a permis de découvrir la recette du premier
  import ci-dessus.

Cas qui méritent un test, parce qu'ils sont contre-intuitifs et déjà constatés :

- pagination s'arrêtant sur une page vide alors qu'une clé de continuation est présente ;
- clé de continuation envoyée sans les paramètres d'origine ;
- flux reçu dans le désordre ;
- transaction sans date de comptabilisation ; transaction en devise étrangère ;
- transaction sans référence ; deux transactions de comptes différents partageant la même référence ;
- même opération présente sur le compte carte et le compte courant ;
- opération passant d'« en attente » à « comptabilisée » avec changement de référence ;
- **amorçage sur un compte possédant plusieurs années d'historique manuel** : seule la fenêtre est
  remplacée, rien d'antérieur n'est perdu ;
- réconciliation qui ne tombe pas juste : l'écart est détecté, daté et signalé, jamais absorbé ;
- compte lié exclu de la projection, avec avancement correct de `balance_updated_at` ;
- retour de parcours portant un refus ou une annulation au lieu d'un code ;
- `state` inconnu ou rejoué ;
- consentement expiré détecté sans perte du rattachement ;
- second appel de synchro le même jour : sans effet.
