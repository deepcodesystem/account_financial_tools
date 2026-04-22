from odoo import _, fields, models


class TvaRasWizard(models.TransientModel):
    _name = 'tva.ras.wizard'
    _description = 'Assistant Déclaration TVA RAS'

    annee = fields.Char(string='Année', required=True, default=lambda self: str(fields.Date.today().year))
    periode = fields.Selection(
        [(str(i), str(i)) for i in range(1, 13)],
        string='Période (Mois)',
        required=True,
        default=lambda self: str(fields.Date.today().month),
    )
    company_id = fields.Many2one('res.company', string='Société', required=True, default=lambda self: self.env.company)

    def action_create_declaration(self):
        self.ensure_one()
        declaration = self.env['tva.ras.declaration'].create({
            'name': _('Déclaration TVA RAS %(periode)s/%(annee)s', periode=self.periode, annee=self.annee),
            'company_id': self.company_id.id,
            'annee': self.annee,
            'periode': self.periode,
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
