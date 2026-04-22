from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    l10n_ma_identifiant_fiscal = fields.Char(string='Identifiant Fiscal (IF)', size=8)
    l10n_ma_ice = fields.Char(string='ICE (Identifiant Commun Entreprise)', size=15)
    l10n_ma_regime_tva = fields.Selection(
        [('1', 'Débit'), ('2', 'Encaissement')],
        string='Régime TVA',
        default='1',
    )
