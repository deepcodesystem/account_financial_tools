from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    l10n_ma_rd_if = fields.Char(string='IF Fournisseur', size=8)
    l10n_ma_rd_ice = fields.Char(string='ICE Fournisseur', size=15)
