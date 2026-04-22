from odoo import fields, models


class AccountMove(models.Model):
    _inherit = 'account.move'

    l10n_ma_ref_nat_opt = fields.Selection(
        [('1', 'Type 1'), ('2', 'Type 2'), ('3', 'Type 3')],
        string='Nature Opération (RAS)',
        default='2',
    )
    l10n_ma_taux_retenu_source = fields.Char(
        string='Taux Retenue à la Source (%)',
        default='75',
    )
