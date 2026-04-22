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
REGIME_SELECTION = [('1', 'Débit'), ('2', 'Encaissement')]
MODE_PAIEMENT_SELECTION = [
    ('1', 'Espèces'),
    ('2', 'Chèque'),
    ('3', 'Virement'),
    ('4', 'Effet'),
    ('5', 'Autre'),
]
PERIODICITE_SELECTION = [('monthly', 'Mensuelle'), ('quarterly', 'Trimestrielle')]
QUARTER_TO_MONTH_MAPPING = {'1': '3', '2': '6', '3': '9', '4': '12'}


class TvaRdDeclaration(models.Model):
    _name = 'tva.rd.declaration'
    _description = 'Déclaration Relevé de Déductions TVA'
    _rec_name = 'name'
    _order = 'annee desc, periode desc'

    name = fields.Char(string='Référence', required=True)
    company_id = fields.Many2one(
        'res.company',
        string='Société',
        required=True,
        default=lambda self: self.env.company,
    )
    annee = fields.Char(
        string='Année',
        required=True,
        size=4,
        default=lambda self: str(fields.Date.today().year),
    )
    periodicite = fields.Selection(
        PERIODICITE_SELECTION,
        string='Périodicité',
        required=True,
        default='monthly',
    )
    periode = fields.Selection(
        selection=lambda self: PERIODE_SELECTION + TRIMESTRE_SELECTION,
        string='Période',
        required=True,
    )
    regime = fields.Selection(
        REGIME_SELECTION,
        string='Régime TVA',
        required=True,
        default='1',
    )
    state = fields.Selection(
        [('draft', 'Brouillon'), ('validated', 'Validé'), ('exported', 'Exporté')],
        string='État',
        default='draft',
        required=True,
    )
    line_ids = fields.One2many('tva.rd.declaration.line', 'declaration_id', string='Lignes')
    line_count = fields.Integer(string='Nb Lignes', compute='_compute_line_count')

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.constrains('annee')
    def _check_annee(self):
        for rec in self:
            if rec.annee and (len(rec.annee) != 4 or not rec.annee.isdigit()):
                raise ValidationError(_("L'année doit contenir exactement 4 chiffres."))

    @api.constrains('periodicite', 'periode')
    def _check_periode_periodicite(self):
        for rec in self:
            if not rec.periode:
                continue
            try:
                period = int(rec.periode)
            except (TypeError, ValueError):
                raise ValidationError(_('Période invalide.')) from None
            if rec.periodicite == 'quarterly' and (period < 1 or period > 4):
                raise ValidationError(_('En mode trimestriel, la période doit être comprise entre 1 et 4.'))
            if rec.periodicite == 'monthly' and (period < 1 or period > 12):
                raise ValidationError(_('En mode mensuel, la période doit être comprise entre 1 et 12.'))

    def _get_period_dates(self):
        self.ensure_one()
        try:
            year = int(self.annee)
            period = int(self.periode)
        except (TypeError, ValueError):
            raise UserError(_('Année ou période invalide.')) from None

        if self.periodicite == 'quarterly':
            start_month = (period - 1) * 3 + 1
            end_month = start_month + 3
            start_date = date(year, start_month, 1)
            end_date = date(year + 1, 1, 1) if end_month > 12 else date(year, end_month, 1)
        else:
            start_date = date(year, period, 1)
            end_date = date(year + 1, 1, 1) if period == 12 else date(year, period + 1, 1)
        return start_date, end_date

    def _get_payment_date(self, move):
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

    def _get_mode_paiement(self, move):
        payment_moves = self.env['account.move']
        payable_lines = move.line_ids.filtered(
            lambda l: l.account_id.account_type == 'liability_payable' and l.reconciled
        )
        for line in payable_lines:
            for match in line.matched_credit_ids:
                if match.credit_move_id.move_id != move:
                    payment_moves |= match.credit_move_id.move_id
            for match in line.matched_debit_ids:
                if match.debit_move_id.move_id != move:
                    payment_moves |= match.debit_move_id.move_id

        if payment_moves.filtered(lambda m: m.journal_id.type == 'cash'):
            return '1'

        bank_moves = payment_moves.filtered(lambda m: m.journal_id.type == 'bank')
        for payment_move in bank_moves:
            payment = payment_move.payment_id
            method_name = (
                payment.payment_method_line_id.name.lower()
                if payment and payment.payment_method_line_id and payment.payment_method_line_id.name
                else ''
            )
            if any(keyword in method_name for keyword in ('chèque', 'cheque', 'check')):
                return '2'
        if bank_moves:
            return '3'
        return '5'

    def action_generate_lines(self):
        for rec in self:
            if not rec.annee or not rec.periode:
                raise UserError(_('Veuillez renseigner l\'année et la période.'))

            start_date, end_date = rec._get_period_dates()
            moves = self.env['account.move'].search([
                ('company_id', '=', rec.company_id.id),
                ('move_type', '=', 'in_invoice'),
                ('state', '=', 'posted'),
                ('payment_state', 'in', ['paid', 'in_payment']),
                ('invoice_date', '>=', start_date),
                ('invoice_date', '<', end_date),
            ], order='invoice_date asc, id asc')

            lines_vals = [(5, 0, 0)]
            seq = 1
            for move in moves:
                grouped_amounts = {}
                for inv_line in move.invoice_line_ids.filtered(lambda l: not l.display_type):
                    taxes = inv_line.tax_ids.filtered(lambda t: t.amount_type == 'percent' and t.amount > 0)
                    rate = round(taxes[0].amount, 2) if taxes else 0.0
                    grouped_amounts[rate] = grouped_amounts.get(rate, 0.0) + inv_line.price_subtotal

                if not grouped_amounts:
                    grouped_amounts[0.0] = move.amount_untaxed

                payment_date = rec._get_payment_date(move)
                mode_paiement = rec._get_mode_paiement(move)
                num_facture = (move.ref or move.name or '')[:50]
                description = (
                    move.narration or move.invoice_payment_ref or move.ref or move.name or ''
                )[:255]

                for rate in sorted(grouped_amounts):
                    lines_vals.append((0, 0, {
                        'move_id': move.id,
                        'ord': seq,
                        'num_facture': num_facture,
                        'description': description,
                        'montant_ht': round(grouped_amounts[rate], 2),
                        'if_fournisseur': (move.partner_id.l10n_ma_rd_if or '')[:8],
                        'nom_fournisseur': move.partner_id.name or '',
                        'ice_fournisseur': (move.partner_id.l10n_ma_rd_ice or '')[:15],
                        'taux_tva': rate,
                        'mode_paiement': mode_paiement,
                        'date_paiement': payment_date,
                        'date_facture': move.invoice_date,
                    }))
                    seq += 1
            rec.line_ids = lines_vals
        return True

    def action_validate(self):
        for rec in self:
            if not rec.company_id.l10n_ma_rd_identifiant_fiscal:
                raise UserError(_("L'identifiant fiscal de la société est obligatoire."))
            if not rec.line_ids:
                raise UserError(_('La déclaration ne contient aucune ligne.'))
            rec.state = 'validated'
        return True

    def action_reset_draft(self):
        for rec in self:
            rec.state = 'draft'
        return True

    def _get_periode_xml(self):
        self.ensure_one()
        if self.periodicite == 'quarterly':
            return QUARTER_TO_MONTH_MAPPING.get(self.periode, self.periode)
        return self.periode

    def _generate_xml_content(self):
        self.ensure_one()
        if not self.company_id.l10n_ma_rd_identifiant_fiscal:
            raise UserError(_("L'identifiant fiscal de la société est obligatoire."))

        root = etree.Element('DeclarationReleveDeduction')
        etree.SubElement(root, 'identifiantFiscal').text = (
            self.company_id.l10n_ma_rd_identifiant_fiscal or ''
        )[:8]
        etree.SubElement(root, 'annee').text = self.annee
        etree.SubElement(root, 'periode').text = self._get_periode_xml()
        etree.SubElement(root, 'regime').text = self.regime

        releve = etree.SubElement(root, 'releveDeductions')
        for line in self.line_ids.sorted('ord'):
            rd = etree.SubElement(releve, 'rd')
            etree.SubElement(rd, 'ord').text = str(line.ord or 0)
            etree.SubElement(rd, 'num').text = (line.num_facture or '')[:50]
            etree.SubElement(rd, 'des').text = line.description or ''
            etree.SubElement(rd, 'mht').text = f"{(line.montant_ht or 0.0):.2f}"
            etree.SubElement(rd, 'tva').text = f"{(line.montant_tva or 0.0):.2f}"
            etree.SubElement(rd, 'ttc').text = f"{(line.montant_ttc or 0.0):.2f}"
            ref_f = etree.SubElement(rd, 'refF')
            etree.SubElement(ref_f, 'if').text = (line.if_fournisseur or '')[:8]
            etree.SubElement(ref_f, 'nom').text = line.nom_fournisseur or ''
            etree.SubElement(ref_f, 'ice').text = (line.ice_fournisseur or '')[:15]
            etree.SubElement(rd, 'tx').text = f"{(line.taux_tva or 0.0):.2f}"
            etree.SubElement(rd, 'prorata').text = str(
                line.prorata if line.prorata is not None else 100
            )
            mp = etree.SubElement(rd, 'mp')
            etree.SubElement(mp, 'id').text = line.mode_paiement or '5'
            etree.SubElement(rd, 'dpai').text = str(line.date_paiement) if line.date_paiement else ''
            etree.SubElement(rd, 'dfac').text = str(line.date_facture) if line.date_facture else ''

        return etree.tostring(root, xml_declaration=True, encoding='UTF-8', pretty_print=True)

    def _get_export_filename_base(self):
        self.ensure_one()
        try:
            year = int(self.annee)
            month = int(self._get_periode_xml())
        except (TypeError, ValueError):
            raise UserError(_('Année ou période invalide pour l\'export.')) from None
        return f"RD_TVA_{self.company_id.id}_{year}_{month:02d}"

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
        attachment = self._create_attachment(f"{base_name}.zip", buffer.getvalue(), 'application/zip')
        self.state = 'exported'
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }


class TvaRdDeclarationLine(models.Model):
    _name = 'tva.rd.declaration.line'
    _description = 'Ligne Relevé de Déductions TVA'
    _order = 'ord asc, id asc'

    declaration_id = fields.Many2one(
        'tva.rd.declaration',
        string='Déclaration',
        required=True,
        ondelete='cascade',
    )
    move_id = fields.Many2one('account.move', string='Facture')
    ord = fields.Integer(string='N° Ordre')
    num_facture = fields.Char(string='N° Facture', size=50)
    description = fields.Char(string='Description')
    montant_ht = fields.Float(string='Montant HT', digits=(16, 2))
    montant_tva = fields.Float(
        string='Montant TVA',
        digits=(16, 2),
        compute='_compute_montants',
        store=True,
    )
    montant_ttc = fields.Float(
        string='Montant TTC',
        digits=(16, 2),
        compute='_compute_montants',
        store=True,
    )
    if_fournisseur = fields.Char(string='IF Fournisseur', size=8)
    nom_fournisseur = fields.Char(string='Nom Fournisseur')
    ice_fournisseur = fields.Char(string='ICE Fournisseur', size=15)
    taux_tva = fields.Float(string='Taux TVA (%)', default=20.0)
    prorata = fields.Integer(string='Prorata (%)', default=100)
    mode_paiement = fields.Selection(
        MODE_PAIEMENT_SELECTION,
        string='Mode Paiement',
        default='3',
    )
    date_paiement = fields.Date(string='Date Paiement')
    date_facture = fields.Date(string='Date Facture')

    @api.depends('montant_ht', 'taux_tva')
    def _compute_montants(self):
        for line in self:
            taux_tva = line.taux_tva or 0.0
            montant_tva = round((line.montant_ht or 0.0) * taux_tva / 100, 2)
            line.montant_tva = montant_tva
            line.montant_ttc = round((line.montant_ht or 0.0) + montant_tva, 2)
