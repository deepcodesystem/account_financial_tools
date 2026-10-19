from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .res_partner import normalize_ma_identifier


class ResCompany(models.Model):
    _inherit = 'res.company'

    l10n_ma_rd_regime_tva = fields.Selection(
        [('1', 'Débit'), ('2', 'Encaissement')],
        string='Régime de comptabilisation',
        default='1',
    )
    l10n_ma_rd_periodicite = fields.Selection(
        [('monthly', 'Mensuelle'), ('quarterly', 'Trimestrielle')],
        string='Périodicité',
        default='monthly',
    )
    l10n_ma_rd_prorata = fields.Float(
        string='Prorata de déduction (%)',
        digits=(16, 2),
        default=100.0,
    )
    l10n_ma_rd_cash_daily_limit = fields.Float(
        string='Plafond espèces par jour et par fournisseur (TTC)',
        digits=(16, 2),
        default=5000.0,
        help="Art. 106 CGI : au-delà, la TVA des achats réglés en espèces n'est pas déductible. "
             "Les lignes concernées sont signalées dans le relevé. 0 : contrôle désactivé.",
    )
    l10n_ma_rd_cash_monthly_limit = fields.Float(
        string='Plafond espèces par mois et par fournisseur (TTC)',
        digits=(16, 2),
        default=50000.0,
        help="Art. 106 CGI : cumul mensuel des règlements en espèces au-delà duquel la TVA "
             "n'est pas déductible. 0 : contrôle désactivé.",
    )
    l10n_ma_ras_default_nature = fields.Selection(
        [('1', "Achat de biens d'équipement"), ('2', 'Achat de travaux'), ('3', 'Achat de services')],
        string='Nature RAS par défaut (lignes sans article)',
        default='3',
        help="Nature RAS TVA proposée sur les lignes de facture fournisseur sans article "
             "ni compte d'immobilisation. Vide : lignes non soumises.",
    )

    @api.constrains('l10n_ma_rd_prorata')
    def _check_l10n_ma_rd_prorata(self):
        for company in self:
            if not 0.0 < company.l10n_ma_rd_prorata <= 100.0:
                raise ValidationError(_("Le prorata de déduction doit être compris entre 0 (exclu) et 100."))

    def _l10n_ma_get_if(self):
        """IF de la société déclarante, repris de son N° TVA."""
        self.ensure_one()
        return normalize_ma_identifier(self.vat)
