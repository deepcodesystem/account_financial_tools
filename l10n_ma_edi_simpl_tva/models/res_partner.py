import re

from odoo import api, fields, models


def normalize_ma_identifier(value):
    """Retire le préfixe MA, les espaces, points et tirets d'un IF / ICE saisi librement."""
    value = re.sub(r'[\s.\-]', '', value or '').upper()
    return value[2:] if value.startswith('MA') else value


class ResPartner(models.Model):
    _inherit = 'res.partner'

    l10n_ma_ras_applicable = fields.Boolean(
        string='Soumis à la RAS TVA',
        help="Les factures de ce fournisseur (travaux, services, biens d'équipement) sont soumises "
             "à la retenue à la source de la TVA.",
    )
    l10n_ma_ras_taux = fields.Selection(
        [('75', '75 %'), ('100', '100 %')],
        string='Taux RAS TVA',
        compute='_compute_l10n_ma_ras_taux', store=True, readonly=False,
        help="75 % pour une personne morale, 100 % pour une personne physique (par défaut, à vérifier).",
    )

    def _l10n_ma_is_moroccan(self):
        """Partenaire établi au Maroc (ou sans pays renseigné)."""
        self.ensure_one()
        country = self.commercial_partner_id.country_id
        return not country or country.code == 'MA'

    def _l10n_ma_get_if(self):
        """Identifiant fiscal (IF) repris du N° TVA, sans préfixe pays ni séparateurs.
        Vide pour un partenaire étranger : son N° TVA n'est pas un IF marocain."""
        self.ensure_one()
        partner = self.commercial_partner_id
        if not partner._l10n_ma_is_moroccan():
            return ''
        return normalize_ma_identifier(partner.vat)

    def _l10n_ma_get_ice(self):
        """ICE repris du champ ICE de l10n_ma (company_registry). Vide pour un partenaire étranger."""
        self.ensure_one()
        partner = self.commercial_partner_id
        if not partner._l10n_ma_is_moroccan():
            return ''
        return normalize_ma_identifier(partner.company_registry)

    @api.depends('is_company')
    def _compute_l10n_ma_ras_taux(self):
        for partner in self:
            partner.l10n_ma_ras_taux = partner.l10n_ma_ras_taux or ('75' if partner.is_company else '100')

    def action_l10n_ma_rd_save_and_refresh(self):
        """Bouton de la fiche fournisseur simplifiée : met à jour la déclaration d'origine."""
        context = self.env.context
        for model, key in (
            ('tva.rd.declaration', 'l10n_ma_rd_declaration_id'),
            ('tva.ras.declaration', 'l10n_ma_ras_declaration_id'),
        ):
            declaration = self.env[model].browse(context.get(key)).exists()
            declaration._refresh_supplier_data(partners=self)
        return {'type': 'ir.actions.act_window_close'}
