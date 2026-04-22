import base64
import io
import zipfile
from datetime import date

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


PERIODE_SELECTION = [(str(i), str(i)) for i in range(1, 13)]
REGIME_SELECTION = [('1', 'Débit'), ('2', 'Encaissement')]
REF_NAT_OPT_SELECTION = [('1', 'Type 1'), ('2', 'Type 2'), ('3', 'Type 3')]


class TvaRasDeclaration(models.Model):
    _name = 'tva.ras.declaration'
    _description = 'Déclaration TVA RAS'

    name = fields.Char(string='Référence', required=True, default=lambda self: _('Nouvelle déclaration'))
    company_id = fields.Many2one('res.company', string='Société', required=True, default=lambda self: self.env.company)
    annee = fields.Char(string='Année', required=True, size=4, default=lambda self: str(fields.Date.today().year))
    periode = fields.Selection(PERIODE_SELECTION, string='Période (Mois)', required=True)
    regime = fields.Selection(REGIME_SELECTION, string='Régime TVA', required=True, default='1')
    state = fields.Selection(
        [('draft', 'Brouillon'), ('validated', 'Validé'), ('exported', 'Exporté')],
        string='État',
        default='draft',
        required=True,
    )
    line_ids = fields.One2many('tva.ras.declaration.line', 'declaration_id', string='Lignes')

    @api.constrains('annee')
    def _check_annee(self):
        for rec in self:
            if rec.annee and (len(rec.annee) != 4 or not rec.annee.isdigit()):
                raise ValidationError(_('L\'année doit contenir exactement 4 chiffres.'))

    def action_generate_lines(self):
        for rec in self:
            if not rec.periode or not rec.annee:
                raise UserError(_('Veuillez renseigner la période et l\'année.'))
            month = int(rec.periode)
            year = int(rec.annee)
            start_date = date(year, month, 1)
            if month == 12:
                end_date = date(year + 1, 1, 1)
            else:
                end_date = date(year, month + 1, 1)

            moves = self.env['account.move'].search([
                ('company_id', '=', rec.company_id.id),
                ('move_type', '=', 'in_invoice'),
                ('state', '=', 'posted'),
                ('invoice_date', '>=', start_date),
                ('invoice_date', '<', end_date),
            ])

            lines_vals = []
            for move in moves:
                lines_vals.append((0, 0, {
                    'move_id': move.id,
                    'ifu_fournisseur': (move.partner_id.l10n_ma_ifu or '')[:8],
                    'num_facture': (move.ref or move.name or '')[:50],
                    'date_paiement': move.invoice_date,
                    'date_operation': move.invoice_date,
                    'ref_nat_opt': move.l10n_ma_ref_nat_opt or '2',
                    'montant_ht': move.amount_untaxed,
                    'taux_tva': rec._get_taux_tva(move),
                    'taux_retenu_source': move.l10n_ma_taux_retenu_source or '75',
                }))
            rec.line_ids = [(5, 0, 0)] + lines_vals
        return True

    def _get_taux_tva(self, move):
        taxes = move.invoice_line_ids.mapped('tax_ids').filtered(lambda t: t.amount_type == 'percent')
        if not taxes:
            return ''
        amount = taxes[0].amount
        if float(amount).is_integer():
            return str(int(amount))
        return str(amount)

    def action_validate(self):
        for rec in self:
            if not rec.company_id.l10n_ma_identifiant_fiscal:
                raise UserError(_('L\'identifiant fiscal de la société est obligatoire.'))
            rec.state = 'validated'
        return True

    def _generate_xml_content(self):
        self.ensure_one()
        xml_bytes = self.env['ir.qweb']._render('l10n_ma_edi_tva.report_tva_ras_xml', {'docs': self})
        if isinstance(xml_bytes, str):
            xml_bytes = xml_bytes.encode('utf-8')
        if not xml_bytes.lstrip().startswith(b'<?xml'):
            xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes
        return xml_bytes

    def action_export_xml(self):
        self.ensure_one()
        xml_content = self._generate_xml_content()
        filename = f"TVA_RAS_{self.company_id.id}_{self.annee}_{self.periode}.xml"
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'datas': base64.b64encode(xml_content),
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'application/xml',
        })
        self.state = 'exported'
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    def action_export_zip(self):
        self.ensure_one()
        xml_content = self._generate_xml_content()
        xml_filename = f"TVA_RAS_{self.company_id.id}_{self.annee}_{self.periode}.xml"
        zip_filename = f"TVA_RAS_{self.company_id.id}_{self.annee}_{self.periode}.zip"

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(xml_filename, xml_content)

        attachment = self.env['ir.attachment'].create({
            'name': zip_filename,
            'datas': base64.b64encode(buffer.getvalue()),
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'application/zip',
        })
        self.state = 'exported'
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }


class TvaRasDeclarationLine(models.Model):
    _name = 'tva.ras.declaration.line'
    _description = 'Ligne Déclaration TVA RAS'

    declaration_id = fields.Many2one('tva.ras.declaration', string='Déclaration', required=True, ondelete='cascade')
    move_id = fields.Many2one('account.move', string='Facture')
    ifu_fournisseur = fields.Char(string='IF Fournisseur', size=8)
    num_facture = fields.Char(string='Numéro Facture', size=50)
    date_paiement = fields.Date(string='Date Paiement')
    date_operation = fields.Date(string='Date Opération')
    ref_nat_opt = fields.Selection(REF_NAT_OPT_SELECTION, string='Nature Opération', default='2')
    montant_ht = fields.Float(string='Montant HT')
    taux_tva = fields.Char(string='Taux TVA')
    taux_retenu_source = fields.Char(string='Taux Retenue Source', default='75')
