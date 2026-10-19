from odoo import fields, models

from .tva_ras_declaration import RAS_NATURE_SELECTION


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    l10n_ma_ras_nature = fields.Selection(
        RAS_NATURE_SELECTION,
        string='Nature RAS TVA',
        help="Nature de l'opération déclarée pour la retenue à la source de la TVA. "
             "Vide : services → « Achat de services », biens d'équipement à préciser, "
             "marchandises non soumises.",
    )
