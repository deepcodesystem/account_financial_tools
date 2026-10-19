# Maroc - EDI SIMPL-TVA (Relevé des déductions, RAS TVA)

Module Odoo 18 générant les fichiers XML EDI du télé-service SIMPL-TVA (DGI Maroc) :

- **relevé des déductions TVA** ;
- **TVA retenue à la source** (bordereau d'avis de versement, cahier des charges V2.0 du 19/08/2024).

Conformité :
- Cahier des charges EDI SIMPL-TVA **Relevé des déductions V2.0** (30/11/2017)
- **Annexe 1** — correspondance des balises RD **V4.0** (`<regime>` : 1 = mensuel, 2 = trimestriel, 3/4 = cessation/cession hors périmètre)
- **Annexe 2** — exemple de fichier XML (`annexe2_EDI_releveDeduction_Exemple_V2.0.xml`)

## Fonctionnalités

- Création d'une déclaration par société / année / période (mensuelle ou trimestrielle), avec assistant.
- Périodicité **personnalisée** : intervalle de paiements libre (paiements oubliés, déductions reportées), déclaré
  dans une période DGI choisie (mois ou trimestre, balises `<periode>` / `<regime>`). L'intervalle ne peut pas
  dépasser la fin de la période déclarée.
- Anti double déduction : un paiement déjà repris dans une autre déclaration n'est pas regénéré, et la validation
  bloque s'il figure dans deux déclarations.
- Délai de déduction d'un an après le mois du paiement (art. 101-3° CGI) contrôlé à la validation.
- Génération automatique des lignes à partir des factures fournisseurs (`in_invoice`) **et avoirs** (`in_refund`, montants négatifs).
- Sélection par **règlements de la période** (art. 101-2° CGI : le droit à déduction naît au paiement),
  quel que soit le régime de comptabilisation (débit / encaissement, conservé à titre informatif) :

  - une ligne `rd` par paiement et par taux, montants proratisés selon la part payée (paiements partiels) ;
  - montants en devise société (MAD), même pour une facture en devise étrangère ;
  - factures non payées exclues ; lettrages facture/avoir et écarts de change ignorés ;
  - IF / ICE / nom repris du partenaire commercial (société) et non du contact.

- Pièces reprises :

  - factures et avoirs fournisseurs payés dans la période ;
  - **écritures diverses** portant de la TVA d'achat (ex. **frais bancaires à 10 %** saisis au journal de banque) :
    sans compte fournisseur, elles sont considérées réglées à leur date (mode 3 « prélèvement » en banque,
    1 en caisse) ; avec un compte fournisseur, elles suivent le lettrage comme une facture.

- **TVA reprise des lignes de taxe comptabilisées** (pas de recalcul HT x taux) ; base = lignes portant la taxe.
- Exclus : lignes à 0 % / exonérées, TVA non récupérable (répartition hors clôture TVA), jambe négative
  d'autoliquidation, écritures de TVA d'encaissement (CABA) générées par Odoo, écarts de change.
- Le prorata n'est **pas appliqué** aux montants (information à part, défaut 100 %, configurable par société).
- Codes de mode de paiement DGI : 1 espèces, 2 chèque, 3 prélèvement, 4 virement, 5 effet, 6 compensation, 7 autres.
  Détermination, par priorité : mode DGI configuré sur la méthode de paiement du journal ; journal de caisse (1) ;
  compensation (facture client ou écriture diverse soldant fournisseur contre client) (6) ; mots-clés du nom de la
  méthode (chèque, PDC, LCN / effet / traite, prélèvement) ; mode DGI par défaut du journal ; banque (4) ; autres (7).
- **Plafonds espèces (art. 106 CGI)** : les règlements en espèces dépassant le plafond par jour (défaut 5 000 TTC)
  ou par mois (défaut 50 000 TTC) et par fournisseur sont signalés en rouge avec une alerte (non bloquant :
  supprimer les lignes concernées si la TVA n'est pas déductible). Plafonds configurables, 0 = désactivé.
- Export ZIP refusé au-delà de 1 Mo compressé (cahier des charges §2.3.1).
- **IF = N° TVA** de la fiche contact (libellé « IF » pour le Maroc ; préfixe « MA », espaces, points et tirets
  ignorés) et **ICE = champ ICE de `l10n_ma`** (`company_registry`). Rien n'est repris pour un fournisseur établi
  hors du Maroc : son N° TVA n'est pas un IF marocain.
- **Contrôles bloquants** à la validation **et relancés à chaque export** :

  - IF société (chiffres, 8 max) ; unicité de la déclaration par période ;
  - IF et ICE fournisseur obligatoires (sauf fournisseur établi hors du Maroc), formats IF (8 chiffres max) et ICE (15 chiffres) ;
  - taux de TVA légaux uniquement : 7, 10, 14, 20 % ;
  - date de paiement obligatoire et comprise dans la période, non antérieure à la date de facture ;
  - date de facture, mode de paiement, n° de facture, désignation et nom fournisseur obligatoires ;
  - numéros d'ordre strictement positifs et uniques ; prorata dans ]0 ; 100] ;
  - structure XML conforme à l'exemple DGI (balises, formats, TTC = HT + TVA).

- **Workflow verrouillé** : en dehors du brouillon, l'en-tête et les lignes ne sont plus modifiables
  (contrôle côté serveur, pas seulement dans la vue) ; régénération, validation et suppression réservées
  au brouillon ; export réservé aux états Validé / Exporté.
- Export `.xml` et `.zip` (le fichier doit être compressé avant envoi sur SIMPL-TVA).
- Record rules multi-entreprise.

## Configuration

Société (onglet **Maroc EDI RD**) :
- Identifiant Fiscal (IF) déclaré : **N° TVA de la société** (obligatoire, chiffres)
- Régime de comptabilisation (Débit / Encaissement)
- Périodicité par défaut (mensuelle / trimestrielle)
- Prorata de déduction (%) par défaut (100)
- Plafonds espèces par jour / par mois et par fournisseur (art. 106 CGI)

Journaux (Comptabilité → Configuration → Journaux) :

- onglet Paiements sortants / entrants : colonne **Mode DGI** par méthode de paiement (recommandé) ;
- onglet Paramètres avancés : **Mode de paiement DGI par défaut** (ex. frais bancaires saisis au journal de banque).

Partenaire fournisseur (onglet Comptabilité) :
- **IF** : champ N° TVA (8 chiffres au plus)
- **ICE** : champ ICE de `l10n_ma` (15 chiffres)
- pays : un fournisseur établi hors du Maroc est dispensé d'IF et d'ICE

## Utilisation

Comptabilité → Déclarations fiscales → **Nouvelle Déclaration RD TVA** → *Créer et générer* → contrôler les lignes → **Valider** → **Exporter ZIP**.

Compléter un fournisseur : dans les lignes, la colonne **Fournisseur** est un lien vers le contact ; les lignes
incomplètes (IF / ICE manquants) sont en orange. L'icône carte de visite ouvre une **fiche fournisseur simplifiée**
(IF, ICE, pays) ; « Enregistrer et mettre à jour le relevé » reporte les valeurs sur toutes les lignes du fournisseur.
Le bouton **Actualiser les fournisseurs** et la validation reprennent aussi les données des fiches (brouillon uniquement).

## TVA retenue à la source (RAS)

Conformité : cahier des charges EDI SIMPL-TVA **TVA retenue à la source V2.0** (19/08/2024), annexe 1
(correspondance des balises), annexe 2 (exemple) et **annexe 3 (XSD)** : chaque export est validé contre le
schéma officiel (`data/edi/TVARetenueSource_V2.0.xsd`).

Paramétrage :

- fournisseur (onglet Comptabilité ou fiche simplifiée) : **Soumis à la RAS TVA** et **taux** (75 % par défaut
  pour une société, 100 % pour une personne physique — à vérifier selon la situation du fournisseur) ;
- facture fournisseur (onglet Autres informations) : soumission et taux repris du fournisseur, modifiables,
  avec le **montant de TVA à retenir** ; colonne **Nature RAS** sur les lignes (masquée par défaut) ;
- nature de l'opération (`refNatOpt`) : 1 biens d'équipement, 2 travaux, 3 services. Proposée depuis l'article
  (champ « Nature RAS TVA »), à défaut services pour un article de type service, biens d'équipement pour un compte
  d'immobilisation, nature par défaut de la société pour une ligne sans article. Marchandises : non soumises.

Génération (mensuelle, ou personnalisée avec mois déclaré) :

- une ligne par **paiement** de la période et par nature / taux de TVA ;
- la part payée est mesurée sur le **net à payer** (TTC − TVA retenue) : un paiement du net solde la facture ;
- seuls les décaissements (paiements, banque, caisse) comptent : l'écriture constatant la TVA retenue et les
  compensations sont ignorées ;
- un paiement déjà repris dans une autre déclaration RAS n'est pas regénéré (le relevé des déductions reste
  une déclaration distincte).

Contrôles bloquants : IF société et fournisseur, n° de facture, dates, nature, montant HT positif, taux de TVA
admis **selon l'année de l'opération** (2024 : 20, 16, 13, 12, 11, 10, 8 — 2025 : 20, 18, 15, 12, 10, 9 —
2026 : 20, 10), taux de retenue 75 / 100, double déclaration, schéma XSD. ZIP limité à 150 Mo.

Utilisation : Comptabilité → Déclarations fiscales → **Nouvelle Déclaration RAS TVA**.

## Renommage depuis l10n_ma_edi_releve_deductions

Le module s'appelait `l10n_ma_edi_releve_deductions` jusqu'à la version 18.0.2.0.0. Sur une base où l'ancien
nom est installé, exécuter `doc/rename_from_l10n_ma_edi_releve_deductions.sql` (Odoo arrêté, après sauvegarde)
puis mettre à jour `l10n_ma_edi_simpl_tva` : les déclarations et le paramétrage sont conservés.

## Reprise des anciens champs IF / ICE (18.0.2.1.0)

Jusqu'à la 18.0.2.0.x, le module avait ses propres champs IF / ICE (contact et société). La mise à jour les
recopie dans le N° TVA et le champ ICE **lorsque ceux-ci sont vides**, puis les supprime ; une valeur déjà saisie
n'est jamais écrasée (les divergences sont signalées dans le journal d'Odoo).

## Version

18.0.2.1.1
