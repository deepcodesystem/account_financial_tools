from odoo import fields, models

from .tva_rd_declaration import MODE_PAIEMENT_SELECTION


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    l10n_ma_rd_mode_paiement = fields.Selection(
        MODE_PAIEMENT_SELECTION,
        string='Mode de paiement DGI par défaut',
        help="Relevé des déductions TVA : mode retenu pour les écritures de ce journal "
             "(frais bancaires, règlements sans méthode de paiement configurée). "
             "Vide : espèces pour une caisse, prélèvement pour une écriture directe en banque, "
             "virement pour un règlement en banque.",
    )


class AccountPaymentMethodLine(models.Model):
    _inherit = 'account.payment.method.line'

    l10n_ma_rd_mode_paiement = fields.Selection(
        MODE_PAIEMENT_SELECTION,
        string='Mode DGI',
        help="Relevé des déductions TVA : mode de paiement déclaré pour les règlements "
             "effectués avec cette méthode (prioritaire sur toute détection automatique).",
    )
