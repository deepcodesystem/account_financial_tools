from odoo import _, fields, models
from odoo.exceptions import UserError


class TvaRasWizard(models.TransientModel):
    _name = 'tva.ras.wizard'
    _description = 'Assistant Déclaration TVA RAS'

    annee = fields.Char(
        string='Année', required=True, size=4,
        default=lambda self: str(fields.Date.today().year),
    )
    periodicite = fields.Selection(
        [('monthly', 'Mensuelle'), ('quarterly', 'Trimestrielle')],
        string='Périodicité',
        required=True,
        default=lambda self: self.env.company.l10n_ma_periodicite_tva or 'monthly',
    )
    periode_mensuelle = fields.Selection(
        [(str(i), str(i)) for i in range(1, 13)],
        string='Mois',
        default=lambda self: str(fields.Date.today().month),
    )
    periode_trimestrielle = fields.Selection(
        [('1', 'T1 (Jan-Mar)'), ('2', 'T2 (Avr-Jun)'), ('3', 'T3 (Jul-Sep)'), ('4', 'T4 (Oct-Déc)')],
        string='Trimestre',
        default='1',
    )
    company_id = fields.Many2one(
        'res.company', string='Société', required=True,
        default=lambda self: self.env.company,
    )

    def action_create_declaration(self):
        self.ensure_one()
        if not self.annee or len(self.annee) != 4 or not self.annee.isdigit():
            raise UserError(_("L'année doit contenir exactement 4 chiffres."))
        if self.periodicite == 'monthly':
            if not self.periode_mensuelle:
                raise UserError(_("Veuillez renseigner le mois pour une déclaration mensuelle."))
            periode = self.periode_mensuelle
            try:
                periode_label = f"{int(periode):02d}"
            except (TypeError, ValueError):
                raise UserError(_("Mois invalide pour une déclaration mensuelle.")) from None
            declaration_name = _('TVA RAS %(periode)s/%(annee)s', periode=periode_label, annee=self.annee)
        else:
            if not self.periode_trimestrielle:
                raise UserError(_("Veuillez renseigner le trimestre pour une déclaration trimestrielle."))
            periode = self.periode_trimestrielle
            declaration_name = _('TVA RAS T%(periode)s/%(annee)s', periode=periode, annee=self.annee)

        declaration = self.env['tva.ras.declaration'].create({
            'name': declaration_name,
            'company_id': self.company_id.id,
            'annee': self.annee,
            'periode': periode,
            'periodicite': self.periodicite,
            'regime': self.company_id.l10n_ma_regime_tva or '1',
        })
        declaration.action_generate_lines()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Déclaration TVA RAS'),
            'res_model': 'tva.ras.declaration',
            'view_mode': 'form',
            'res_id': declaration.id,
            'target': 'current',
        }
