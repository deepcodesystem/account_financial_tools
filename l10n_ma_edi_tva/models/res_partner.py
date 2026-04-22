from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    l10n_ma_ifu = fields.Char(string='IF Fournisseur (IFU)', size=8)
