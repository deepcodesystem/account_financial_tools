from datetime import date

from lxml import etree

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.misc import file_path

from .edi_declaration import get_vat_rates_for_year


RAS_NATURE_SELECTION = [
    ('1', "Achat de biens d'équipement"),
    ('2', 'Achat de travaux'),
    ('3', 'Achat de services'),
]
RAS_TAUX_SELECTION = [('75', '75 %'), ('100', '100 %')]
RAS_REGIME = '1'  # Annexe 1 : le bordereau de versement de la RAS TVA est mensuel
# Cahier des charges RAS V2.0 §2.3.1 : 150 Mo maximum pour le fichier compressé
MAX_RAS_ZIP_SIZE = 150 * 1024 * 1024
RAS_XSD_PATH = 'l10n_ma_edi_simpl_tva/data/edi/TVARetenueSource_V2.0.xsd'

_ras_schema = None


def _get_ras_schema():
    global _ras_schema
    if _ras_schema is None:
        with open(file_path(RAS_XSD_PATH), 'rb') as xsd_file:
            _ras_schema = etree.XMLSchema(etree.parse(xsd_file))
    return _ras_schema


class TvaRasDeclaration(models.Model):
    _name = 'tva.ras.declaration'
    _inherit = 'l10n.ma.edi.declaration'
    _description = 'Déclaration TVA retenue à la source'
    _rec_name = 'name'
    _order = 'annee desc, id desc'

    _sql_constraints = [
        ('company_annee_periode_uniq', 'unique(company_id, annee, periode)',
         'Une déclaration RAS existe déjà pour cette société, cette année et ce mois.'),
    ]

    line_ids = fields.One2many('tva.ras.declaration.line', 'declaration_id', string='Lignes')
    line_count = fields.Integer(string='Nb Lignes', compute='_compute_line_count')
    currency_id = fields.Many2one(related='company_id.currency_id')
    montant_retenu_total = fields.Monetary(
        string='TVA retenue à verser',
        compute='_compute_montant_retenu_total',
        currency_field='currency_id',
    )

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.depends('line_ids.montant_retenu')
    def _compute_montant_retenu_total(self):
        for rec in self:
            rec.montant_retenu_total = sum(rec.line_ids.mapped('montant_retenu'))

    @api.constrains('periodicite_declaree')
    def _check_monthly(self):
        for rec in self:
            if rec.periodicite_declaree != 'monthly':
                raise ValidationError(_("La déclaration de la TVA retenue à la source est mensuelle."))

    # ------------------------------------------------------------------
    # Points d'extension du socle
    # ------------------------------------------------------------------

    @api.model
    def _selection_periodicite(self):
        # Le bordereau d'avis de versement de la TVA retenue à la source est mensuel
        return [('monthly', 'Mensuelle'), ('custom', 'Personnalisée')]

    @api.model
    def _selection_periodicite_declaree(self):
        return [('monthly', 'Mensuelle')]

    def _get_default_declared_periodicite(self):
        return 'monthly'

    def _get_edi_max_zip_size(self):
        return MAX_RAS_ZIP_SIZE

    def _get_export_prefix(self):
        return 'RAS_TVA'

    def _is_settlement_document(self, move):
        """Facture fournisseur soumise à la retention, avec des lignes RAS taxées."""
        return bool(
            move.state == 'posted'
            and move.move_type == 'in_invoice'
            and move.l10n_ma_ras_applicable
            and self._get_ras_amounts(move)
        )

    def _is_settlement_payment(self, move):
        """Seuls les décaissements (paiements, banque, caisse) déclenchent la retenue : l'écriture
        constatant la TVA retenue et les compensations ne sont pas des paiements."""
        if move.move_type in ('in_invoice', 'in_refund', 'out_invoice', 'out_refund'):
            return False
        return bool(move.origin_payment_id) or move.journal_id.type in ('bank', 'cash')

    def _before_validate(self):
        return self._refresh_supplier_data()

    # ------------------------------------------------------------------
    # Génération
    # ------------------------------------------------------------------

    def _get_ras_amounts(self, move):
        """{(nature, taux TVA): HT} des lignes soumises à la retenue, en devise société."""
        grouped = {}
        for line in move.invoice_line_ids.filtered(
            lambda l: l.display_type == 'product' and l.l10n_ma_ras_nature
        ):
            taxes = line.tax_ids.filtered(
                lambda t: t.type_tax_use == 'purchase' and t.amount_type == 'percent' and t.amount > 0
            )
            if not taxes:
                continue
            key = (line.l10n_ma_ras_nature, round(taxes[0].amount, 2))
            grouped[key] = grouped.get(key, 0.0) + line.balance
        currency = move.company_id.currency_id
        return {key: amount for key, amount in grouped.items() if not currency.is_zero(amount)}

    def _get_ras_paid_ratio(self, document, document_line, partial, amounts):
        """Part de la facture réglée par ce paiement. Le fournisseur reçoit le TTC diminué de la
        TVA retenue : la part payée se mesure sur ce net à payer."""
        total = self._get_payable_total_currency(document)
        total_company = abs(sum(document.line_ids.filtered(
            lambda l: l.account_id.account_type == 'liability_payable'
        ).mapped('balance')))
        if not total or not total_company:
            return 0.0
        taux = float(document.l10n_ma_ras_taux or 0) / 100.0
        expected_retention = sum(ht * rate / 100.0 * taux for (_nature, rate), ht in amounts.items())
        net_to_pay = total * (1.0 - min(expected_retention / total_company, 1.0))
        if not net_to_pay:
            return 1.0
        return min(self._get_paid_amount_currency(document_line, partial) / net_to_pay, 1.0)

    def _refresh_supplier_data(self, partners=None):
        """Reprend l'IF fournisseur depuis les fiches (brouillons uniquement, valeurs renseignées)."""
        lines = self.filtered(lambda rec: rec.state == 'draft').line_ids.filtered('partner_id')
        if partners is not None:
            lines = lines.filtered(lambda l: l.partner_id.commercial_partner_id in partners.commercial_partner_id)
        for line in lines:
            ifu = line.partner_id._l10n_ma_get_if()
            if ifu and ifu != line.ifu_fournisseur:
                line.ifu_fournisseur = ifu
        return True

    def action_refresh_suppliers(self):
        self._check_state(('draft',), _("Actualiser les fournisseurs"))
        return self._refresh_supplier_data()

    def action_generate_lines(self):
        self._check_state(('draft',), _("Générer les lignes"))
        for rec in self:
            start_date, end_date = rec._get_selection_dates()
            payments = rec._get_period_payments(start_date, end_date)
            declared = rec._get_settlements_declared_elsewhere(
                self.env['account.move'].union(*(payment[0] for payment in payments))
            )
            payments = [p for p in payments if (p[0].id, p[2].move_id.id) not in declared]
            payments.sort(key=lambda p: (p[2].date, p[0].invoice_date or date.min, p[0].id, p[3].id))

            lines_vals = [(5, 0, 0)]
            amounts_cache = {}
            for document, document_line, payment_line, partial in payments:
                if document.id not in amounts_cache:
                    amounts_cache[document.id] = rec._get_ras_amounts(document)
                amounts = amounts_cache[document.id]
                ratio = rec._get_ras_paid_ratio(document, document_line, partial, amounts)
                for (nature, rate), ht in sorted(amounts.items()):
                    lines_vals.append((0, 0, {
                        'move_id': document.id,
                        'payment_move_id': payment_line.move_id.id,
                        'partner_id': document.commercial_partner_id.id,
                        'num_facture': (document.ref or document.name or '')[:50],
                        'date_paiement': payment_line.date,
                        'date_operation': document.invoice_date or document.date,
                        'ref_nat_opt': nature,
                        'montant_ht': round(ht * ratio, 2),
                        'taux_tva': rate,
                        'taux_retenue': document.l10n_ma_ras_taux or '75',
                    }))
            rec.line_ids = lines_vals
        return True

    # ------------------------------------------------------------------
    # Contrôles et export
    # ------------------------------------------------------------------

    def _get_validation_errors(self):
        """Contrôles bloquants avant validation et avant chaque export."""
        self.ensure_one()
        errors = []
        identifiant = self.company_id._l10n_ma_get_if()
        if not identifiant:
            errors.append(_("L'identifiant fiscal de la société est obligatoire (N° TVA de la société)."))
        elif not identifiant.isdigit() or len(identifiant) > 8:
            errors.append(_("L'identifiant fiscal de la société doit contenir uniquement des chiffres (8 maximum)."))
        if not self.line_ids:
            errors.append(_("La déclaration ne contient aucune ligne."))

        start_date, end_date = self._get_selection_dates()
        declared = self._get_settlements_declared_elsewhere(self.line_ids.move_id)
        for index, line in enumerate(self.line_ids, start=1):
            label = _("Ligne %(num)s (%(invoice)s)", num=index, invoice=line.num_facture or '?')
            if not line.ifu_fournisseur:
                errors.append(_("%(label)s : IF du fournisseur manquant.", label=label))
            elif not line.ifu_fournisseur.isdigit() or len(line.ifu_fournisseur) > 8:
                errors.append(_("%(label)s : l'IF du fournisseur doit contenir uniquement des chiffres (8 maximum).", label=label))
            if not line.num_facture:
                errors.append(_("%(label)s : numéro de facture manquant.", label=label))
            if not line.date_operation:
                errors.append(_("%(label)s : date de l'opération manquante.", label=label))
            if not line.date_paiement:
                errors.append(_("%(label)s : date de paiement manquante.", label=label))
            elif not start_date <= line.date_paiement < end_date:
                errors.append(_(
                    "%(label)s : la date de paiement %(date)s est hors de la période déclarée.",
                    label=label, date=line.date_paiement,
                ))
            if not line.ref_nat_opt:
                errors.append(_("%(label)s : nature de l'opération manquante.", label=label))
            if line.montant_ht <= 0:
                errors.append(_("%(label)s : le montant HT doit être positif.", label=label))
            if line.date_operation:
                rates = get_vat_rates_for_year(line.date_operation.year)
                if round(line.taux_tva, 2) not in rates:
                    errors.append(_(
                        "%(label)s : taux de TVA %(rate)s %% non autorisé pour une opération de %(year)s "
                        "(taux admis : %(rates)s).",
                        label=label, rate=f"{line.taux_tva:g}", year=line.date_operation.year,
                        rates=', '.join(f"{r:g}" for r in rates),
                    ))
            if line.taux_retenue not in ('75', '100'):
                errors.append(_("%(label)s : taux de retenue invalide (75 ou 100).", label=label))
            other = declared.get((line.move_id.id, line.payment_move_id.id))
            if line.payment_move_id and other:
                errors.append(_(
                    "%(label)s : ce paiement est déjà déclaré dans %(declaration)s.",
                    label=label, declaration=other.name,
                ))
        return errors

    def _generate_xml_content(self):
        self.ensure_one()
        errors = self._get_validation_errors()
        if errors:
            raise UserError(_("La déclaration comporte des anomalies :\n%s") % "\n".join(errors[:30]))

        root = etree.Element('VersementRetenueSources')
        etree.SubElement(root, 'identifiantFiscal').text = self.company_id._l10n_ma_get_if()
        etree.SubElement(root, 'annee').text = self.annee
        etree.SubElement(root, 'periode').text = self._get_periode_xml()
        etree.SubElement(root, 'regime').text = RAS_REGIME
        fournisseurs = etree.SubElement(root, 'fournisseurs')
        for line in self.line_ids:
            node = etree.SubElement(fournisseurs, 'fournisseur')
            etree.SubElement(node, 'ifuFournisseur').text = line.ifu_fournisseur
            etree.SubElement(node, 'numFacture').text = line.num_facture
            etree.SubElement(node, 'datePaiement').text = str(line.date_paiement)
            etree.SubElement(node, 'dateOperation').text = str(line.date_operation)
            etree.SubElement(node, 'refNatOpt').text = line.ref_nat_opt
            etree.SubElement(node, 'montantHT').text = f"{line.montant_ht:.2f}"
            etree.SubElement(node, 'tauxTva').text = f"{line.taux_tva:g}"
            etree.SubElement(node, 'tauxRetenuSource').text = line.taux_retenue

        schema = _get_ras_schema()
        if not schema.validate(root):
            raise UserError(_(
                "Le fichier EDI n'est pas conforme au schéma XSD de la DGI (annexe 3) :\n%s",
                "\n".join(str(error.message) for error in list(schema.error_log)[:30]),
            ))
        return etree.tostring(root, xml_declaration=True, encoding='UTF-8', pretty_print=True)


class TvaRasDeclarationLine(models.Model):
    _name = 'tva.ras.declaration.line'
    _inherit = 'l10n.ma.edi.declaration.line'
    _description = 'Ligne TVA retenue à la source'
    _order = 'date_paiement asc, id asc'

    declaration_id = fields.Many2one(
        'tva.ras.declaration',
        string='Déclaration',
        required=True,
        ondelete='cascade',
    )
    company_id = fields.Many2one(
        related='declaration_id.company_id',
        store=True,
        index=True,
        readonly=True,
    )
    currency_id = fields.Many2one(related='company_id.currency_id')
    move_id = fields.Many2one('account.move', string='Facture', check_company=True)
    payment_move_id = fields.Many2one('account.move', string='Règlement', check_company=True)
    partner_id = fields.Many2one('res.partner', string='Fournisseur')
    ifu_fournisseur = fields.Char(
        string='IF Fournisseur', size=8,
        compute='_compute_ifu_fournisseur', store=True, readonly=False,
    )
    num_facture = fields.Char(string='N° Facture', size=50)
    date_paiement = fields.Date(string='Date Paiement')
    date_operation = fields.Date(string='Date Opération')
    ref_nat_opt = fields.Selection(RAS_NATURE_SELECTION, string="Nature de l'opération")
    montant_ht = fields.Float(string='Montant HT', digits=(16, 2))
    taux_tva = fields.Float(string='Taux TVA (%)')
    taux_retenue = fields.Selection(RAS_TAUX_SELECTION, string='Taux retenue', default='75')
    montant_tva = fields.Monetary(
        string='Montant TVA', compute='_compute_montants', store=True, currency_field='currency_id',
    )
    montant_retenu = fields.Monetary(
        string='TVA retenue', compute='_compute_montants', store=True, currency_field='currency_id',
    )
    missing_supplier_data = fields.Boolean(
        string='IF fournisseur manquant', compute='_compute_missing_supplier_data',
    )

    @api.depends('partner_id')
    def _compute_ifu_fournisseur(self):
        for line in self:
            if line.partner_id:
                line.ifu_fournisseur = line.partner_id._l10n_ma_get_if() or False
            else:
                line.ifu_fournisseur = line.ifu_fournisseur

    @api.depends('ifu_fournisseur')
    def _compute_missing_supplier_data(self):
        for line in self:
            line.missing_supplier_data = not line.ifu_fournisseur

    @api.depends('montant_ht', 'taux_tva', 'taux_retenue')
    def _compute_montants(self):
        for line in self:
            montant_tva = round(line.montant_ht * line.taux_tva / 100.0, 2)
            line.montant_tva = montant_tva
            line.montant_retenu = round(montant_tva * float(line.taux_retenue or 0) / 100.0, 2)

    @api.constrains('ifu_fournisseur')
    def _check_ifu_fournisseur(self):
        for line in self:
            if line.ifu_fournisseur and not line.ifu_fournisseur.isdigit():
                raise ValidationError(_("L'identifiant fiscal du fournisseur doit contenir uniquement des chiffres."))

    def action_open_partner(self):
        """Fiche fournisseur simplifiée (IF, RAS) en fenêtre, pour compléter les données."""
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_("Aucun fournisseur n'est rattaché à cette ligne."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Fiche fournisseur'),
            'res_model': 'res.partner',
            'res_id': self.partner_id.commercial_partner_id.id,
            'view_mode': 'form',
            'views': [(self.env.ref('l10n_ma_edi_simpl_tva.view_partner_form_l10n_ma_rd_quick').id, 'form')],
            'target': 'new',
            'context': {'l10n_ma_ras_declaration_id': self.declaration_id.id},
        }
