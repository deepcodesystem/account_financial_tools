import base64
import io
import zipfile
from datetime import date

from lxml import etree

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


PERIODE_SELECTION = [(str(i), str(i)) for i in range(1, 13)]
TRIMESTRE_SELECTION = [
    ('1', 'T1 (Jan-Mar)'),
    ('2', 'T2 (Avr-Jun)'),
    ('3', 'T3 (Jul-Sep)'),
    ('4', 'T4 (Oct-Déc)'),
]
QUARTER_TO_MONTH_MAPPING = {'1': '3', '2': '6', '3': '9', '4': '12'}
REGIME_SELECTION = [('1', 'Débit'), ('2', 'Encaissement')]
REF_NAT_OPT_SELECTION = [
    ('1', 'Services'),
    ('2', 'Travaux'),
    ('3', 'Fournitures'),
]


class TvaRasDeclaration(models.Model):
    _name = 'tva.ras.declaration'
    _description = 'Déclaration TVA RAS'
    _rec_name = 'name'
    _order = 'annee desc, periode desc'

    name = fields.Char(string='Référence', required=True, default='New')
    company_id = fields.Many2one(
        'res.company', string='Société', required=True,
        default=lambda self: self.env.company,
    )
    annee = fields.Char(
        string='Année', required=True, size=4,
        default=lambda self: str(fields.Date.today().year),
    )
    periodicite = fields.Selection(
        [('monthly', 'Mensuelle'), ('quarterly', 'Trimestrielle')],
        string='Périodicité',
        required=True,
        default='monthly',
    )
    periode = fields.Selection(
        selection='_get_periode_selection',
        string='Période',
        required=True,
    )
    regime = fields.Selection(REGIME_SELECTION, string='Régime TVA', required=True, default='1')
    state = fields.Selection(
        [('draft', 'Brouillon'), ('validated', 'Validé'), ('exported', 'Exporté')],
        string='État', default='draft', required=True,
    )
    line_ids = fields.One2many('tva.ras.declaration.line', 'declaration_id', string='Lignes')
    line_count = fields.Integer(compute='_compute_line_count', string='Nb Lignes')

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.constrains('annee')
    def _check_annee(self):
        for rec in self:
            if rec.annee and (len(rec.annee) != 4 or not rec.annee.isdigit()):
                raise ValidationError(_("L'année doit contenir exactement 4 chiffres."))

    @api.model
    def _get_periode_selection(self):
        return PERIODE_SELECTION + TRIMESTRE_SELECTION

    @api.constrains('periodicite', 'periode')
    def _check_periode_periodicite(self):
        for rec in self:
            if not rec.periode:
                continue
            try:
                period = int(rec.periode)
            except (TypeError, ValueError):
                raise ValidationError(_("Période invalide.")) from None
            if rec.periodicite == 'quarterly' and (period < 1 or period > 4):
                raise ValidationError(_("En mode trimestriel, la période doit être comprise entre 1 et 4."))
            if rec.periodicite == 'monthly' and (period < 1 or period > 12):
                raise ValidationError(_("En mode mensuel, la période doit être comprise entre 1 et 12."))

    def _get_period_dates(self):
        self.ensure_one()
        try:
            year = int(self.annee)
            period = int(self.periode)
        except (TypeError, ValueError):
            raise UserError(_("Année ou période invalide.")) from None

        if self.periodicite == 'quarterly':
            start_month = (period - 1) * 3 + 1
            end_month = start_month + 3
            start_date = date(year, start_month, 1)
            if end_month > 12:
                end_date = date(year + 1, 1, 1)
            else:
                end_date = date(year, end_month, 1)
        else:
            start_date = date(year, period, 1)
            end_date = date(year + 1, 1, 1) if period == 12 else date(year, period + 1, 1)
        return start_date, end_date

    def _get_payment_date(self, move):
        """Retourne la date de paiement réelle depuis les écritures réconciliées."""
        payable_lines = move.line_ids.filtered(
            lambda l: l.account_id.account_type == 'liability_payable' and l.reconciled
        )
        for line in payable_lines:
            for match in line.matched_credit_ids:
                if match.credit_move_id.move_id != move:
                    return match.credit_move_id.date
            for match in line.matched_debit_ids:
                if match.debit_move_id.move_id != move:
                    return match.debit_move_id.date
        return move.invoice_date

    def _build_lines_vals(self, move):
        """Construit les valeurs des lignes de déclaration pour une facture.
        Génère une ligne par taux de TVA distinct (conformément au XSD).
        """
        payment_date = self._get_payment_date(move)
        ifu = (move.partner_id.l10n_ma_ifu or '')[:8]
        num_facture = (move.ref or move.name or '')[:50]
        ref_nat = move.l10n_ma_ref_nat_opt or '2'
        taux_retenu = move.l10n_ma_taux_retenu_source or '75'

        # Grouper les lignes de facture par taux de TVA
        rate_groups = {}
        for inv_line in move.invoice_line_ids.filtered(lambda l: not l.display_type):
            taxes = inv_line.tax_ids.filtered(
                lambda t: t.amount_type == 'percent' and t.amount > 0
            )
            if taxes:
                rate = str(int(round(taxes[0].amount)))
            else:
                rate = '20'
            rate_groups[rate] = rate_groups.get(rate, 0.0) + inv_line.price_subtotal

        if not rate_groups:
            rate_groups['20'] = move.amount_untaxed

        vals_list = []
        for taux, montant in rate_groups.items():
            vals_list.append({
                'move_id': move.id,
                'ifu_fournisseur': ifu,
                'num_facture': num_facture,
                'date_paiement': payment_date,
                'date_operation': move.invoice_date,
                'ref_nat_opt': ref_nat,
                'montant_ht': round(montant, 2),
                'taux_tva': taux,
                'taux_retenu_source': taux_retenu,
            })
        return vals_list

    def action_generate_lines(self):
        for rec in self:
            if not rec.periode or not rec.annee:
                raise UserError(_("Veuillez renseigner la période et l'année."))

            start_date, end_date = rec._get_period_dates()

            # Régime Débit : filtrage par date de facture
            # Régime Encaissement : filtrage par date de paiement (approximé via invoice_date ici)
            domain = [
                ('company_id', '=', rec.company_id.id),
                ('move_type', '=', 'in_invoice'),
                ('state', '=', 'posted'),
                ('payment_state', 'in', ['paid', 'in_payment']),
                ('invoice_date', '>=', start_date),
                ('invoice_date', '<', end_date),
            ]
            moves = self.env['account.move'].search(domain, order='invoice_date asc')

            lines_vals = [(5, 0, 0)]
            for move in moves:
                for vals in rec._build_lines_vals(move):
                    lines_vals.append((0, 0, vals))

            rec.line_ids = lines_vals
        return True

    def action_validate(self):
        for rec in self:
            if not rec.company_id.l10n_ma_identifiant_fiscal:
                raise UserError(_("L'identifiant fiscal de la société est obligatoire."))
            if not rec.line_ids:
                raise UserError(_("La déclaration ne contient aucune ligne."))
            rec.state = 'validated'
        return True

    def action_reset_draft(self):
        for rec in self:
            rec.state = 'draft'
        return True

    def _generate_xml_content(self):
        """Génère le contenu XML via lxml (conforme XSD Annexe3)."""
        self.ensure_one()
        if not self.company_id.l10n_ma_identifiant_fiscal:
            raise UserError(_("L'identifiant fiscal de la société est obligatoire."))

        root = etree.Element('VersementRetenueSources')
        etree.SubElement(root, 'identifiantFiscal').text = (
            self.company_id.l10n_ma_identifiant_fiscal or ''
        )[:8]
        etree.SubElement(root, 'annee').text = self.annee
        if self.periodicite == 'quarterly':
            periode_xml = QUARTER_TO_MONTH_MAPPING.get(self.periode, self.periode)
        else:
            periode_xml = self.periode
        etree.SubElement(root, 'periode').text = periode_xml
        etree.SubElement(root, 'regime').text = self.regime

        fournisseurs = etree.SubElement(root, 'fournisseurs')
        for line in self.line_ids:
            f = etree.SubElement(fournisseurs, 'fournisseur')
            etree.SubElement(f, 'ifuFournisseur').text = (line.ifu_fournisseur or '')[:8]
            etree.SubElement(f, 'numFacture').text = (line.num_facture or '')[:50]
            etree.SubElement(f, 'datePaiement').text = (
                str(line.date_paiement) if line.date_paiement else ''
            )
            etree.SubElement(f, 'dateOperation').text = (
                str(line.date_operation) if line.date_operation else ''
            )
            etree.SubElement(f, 'refNatOpt').text = line.ref_nat_opt or '2'
            etree.SubElement(f, 'montantHT').text = str(round(line.montant_ht, 2))
            etree.SubElement(f, 'tauxTva').text = line.taux_tva or '20'
            etree.SubElement(f, 'tauxRetenuSource').text = line.taux_retenu_source or '75'

        return etree.tostring(root, xml_declaration=True, encoding='UTF-8', pretty_print=True)

    def _get_export_filename_base(self):
        self.ensure_one()
        try:
            year = int(self.annee)
            month = int(self.periode)
        except (TypeError, ValueError):
            raise UserError(_("Année ou période invalide pour l'export.")) from None
        return f"TVA_RAS_{self.company_id.id}_{year}_{month:02d}"

    def _create_attachment(self, filename, content, mimetype):
        return self.env['ir.attachment'].create({
            'name': filename,
            'datas': base64.b64encode(content),
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': mimetype,
        })

    def action_export_xml(self):
        self.ensure_one()
        xml_content = self._generate_xml_content()
        filename = f"{self._get_export_filename_base()}.xml"
        attachment = self._create_attachment(filename, xml_content, 'application/xml')
        self.state = 'exported'
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    def action_export_zip(self):
        self.ensure_one()
        xml_content = self._generate_xml_content()
        base_name = self._get_export_filename_base()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{base_name}.xml", xml_content)
        attachment = self._create_attachment(
            f"{base_name}.zip", buffer.getvalue(), 'application/zip'
        )
        self.state = 'exported'
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }


class TvaRasDeclarationLine(models.Model):
    _name = 'tva.ras.declaration.line'
    _description = 'Ligne Déclaration TVA RAS'
    _order = 'date_paiement asc, num_facture asc'

    declaration_id = fields.Many2one(
        'tva.ras.declaration', string='Déclaration',
        required=True, ondelete='cascade',
    )
    move_id = fields.Many2one('account.move', string='Facture')
    ifu_fournisseur = fields.Char(string='IFU Fournisseur', size=8)
    num_facture = fields.Char(string='Numéro Facture', size=50)
    date_paiement = fields.Date(string='Date Paiement')
    date_operation = fields.Date(string='Date Opération')
    ref_nat_opt = fields.Selection(REF_NAT_OPT_SELECTION, string='Nature Opération', default='2')
    montant_ht = fields.Float(string='Montant HT', digits=(16, 2))
    taux_tva = fields.Char(string='Taux TVA (%)', default='20')
    taux_retenu_source = fields.Char(string='Taux Retenue Source (%)', default='75')
    montant_tva = fields.Float(
        string='Montant TVA', digits=(16, 2),
        compute='_compute_montants', store=True,
    )
    montant_retenu = fields.Float(
        string='Montant Retenu', digits=(16, 2),
        compute='_compute_montants', store=True,
    )

    @api.depends('montant_ht', 'taux_tva', 'taux_retenu_source')
    def _compute_montants(self):
        for line in self:
            try:
                taux_tva = float(line.taux_tva or 0)
                taux_retenu = float(line.taux_retenu_source or 0)
            except (ValueError, TypeError):
                taux_tva = taux_retenu = 0.0
            line.montant_tva = round(line.montant_ht * taux_tva / 100, 2)
            line.montant_retenu = round(line.montant_tva * taux_retenu / 100, 2)
