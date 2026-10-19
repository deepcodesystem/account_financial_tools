from odoo import api, fields, models

from .tva_ras_declaration import RAS_NATURE_SELECTION, RAS_TAUX_SELECTION


class AccountMove(models.Model):
    _inherit = 'account.move'

    l10n_ma_ras_applicable = fields.Boolean(
        string='Soumise à la RAS TVA',
        compute='_compute_l10n_ma_ras', store=True, readonly=False,
        help="Facture fournisseur soumise à la retenue à la source de la TVA (art. 117-IV CGI). "
             "Reprise de la fiche fournisseur, modifiable sur la facture.",
    )
    l10n_ma_ras_taux = fields.Selection(
        RAS_TAUX_SELECTION,
        string='Taux RAS TVA',
        compute='_compute_l10n_ma_ras', store=True, readonly=False,
    )
    l10n_ma_ras_amount = fields.Monetary(
        string='TVA à retenir',
        compute='_compute_l10n_ma_ras_amount',
        currency_field='currency_id',
        help="TVA des lignes soumises (biens d'équipement, travaux, services) x taux de retenue : "
             "montant à retenir sur le paiement du fournisseur et à verser à la DGI.",
    )

    @api.depends('partner_id', 'move_type')
    def _compute_l10n_ma_ras(self):
        for move in self:
            partner = move.partner_id.commercial_partner_id
            if move.move_type == 'in_invoice' and partner:
                move.l10n_ma_ras_applicable = partner.l10n_ma_ras_applicable
                move.l10n_ma_ras_taux = partner.l10n_ma_ras_taux or '75'
            else:
                move.l10n_ma_ras_applicable = False
                move.l10n_ma_ras_taux = False

    @api.depends(
        'l10n_ma_ras_applicable', 'l10n_ma_ras_taux',
        'invoice_line_ids.l10n_ma_ras_nature', 'invoice_line_ids.price_subtotal', 'invoice_line_ids.tax_ids',
    )
    def _compute_l10n_ma_ras_amount(self):
        for move in self:
            amount = 0.0
            if move.l10n_ma_ras_applicable and move.l10n_ma_ras_taux:
                taux = float(move.l10n_ma_ras_taux) / 100.0
                for line in move.invoice_line_ids.filtered(
                    lambda l: l.display_type == 'product' and l.l10n_ma_ras_nature
                ):
                    taxes = line.tax_ids.filtered(
                        lambda t: t.type_tax_use == 'purchase' and t.amount_type == 'percent' and t.amount > 0
                    )
                    if taxes:
                        amount += line.price_subtotal * taxes[0].amount / 100.0 * taux
            move.l10n_ma_ras_amount = move.currency_id.round(amount) if move.currency_id else amount


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    l10n_ma_ras_nature = fields.Selection(
        RAS_NATURE_SELECTION,
        string='Nature RAS',
        compute='_compute_l10n_ma_ras_nature', store=True, readonly=False,
        help="Nature de l'opération pour la RAS TVA. Vide : ligne non soumise (ex. marchandises).",
    )

    @api.depends('product_id', 'account_id', 'move_id.move_type')
    def _compute_l10n_ma_ras_nature(self):
        for line in self:
            if line.move_id.move_type != 'in_invoice' or line.display_type != 'product':
                line.l10n_ma_ras_nature = False
            elif line.product_id.l10n_ma_ras_nature:
                line.l10n_ma_ras_nature = line.product_id.l10n_ma_ras_nature
            elif line.product_id.type == 'service':
                line.l10n_ma_ras_nature = '3'
            elif line.account_id.account_type == 'asset_fixed':
                line.l10n_ma_ras_nature = '1'
            elif not line.product_id:
                line.l10n_ma_ras_nature = line.company_id.l10n_ma_ras_default_nature or False
            else:
                line.l10n_ma_ras_nature = False
