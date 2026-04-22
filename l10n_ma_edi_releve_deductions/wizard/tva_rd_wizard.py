from odoo import _, fields, models
from odoo.exceptions import UserError

from ..models.tva_rd_declaration import (
    PERIODICITE_SELECTION,
    PERIODE_SELECTION,
    TRIMESTRE_SELECTION,
)


class TvaRdWizard(models.TransientModel):
    _name = 'tva.rd.wizard'
    _description = 'Assistant Déclaration Relevé Déductions TVA'

    annee = fields.Char(
        string='Année',
        required=True,
        size=4,
        default=lambda self: str(fields.Date.today().year),
    )
    periodicite = fields.Selection(
        PERIODICITE_SELECTION,
        string='Périodicité',
        required=True,
        default=lambda self: self.env.company.l10n_ma_rd_periodicite or 'monthly',
    )
    periode_mensuelle = fields.Selection(
        PERIODE_SELECTION,
        string='Mois',
        default=lambda self: str(fields.Date.today().month),
    )
    periode_trimestrielle = fields.Selection(
        TRIMESTRE_SELECTION,
        string='Trimestre',
        default='1',
    )
    company_id = fields.Many2one(
        'res.company',
        string='Société',
        required=True,
        default=lambda self: self.env.company,
    )

    def action_create_declaration(self):
        self.ensure_one()
        if not self.annee or len(self.annee) != 4 or not self.annee.isdigit():
            raise UserError(_("L'année doit contenir exactement 4 chiffres."))

        if self.periodicite == 'monthly':
            if not self.periode_mensuelle:
                raise UserError(_('Veuillez renseigner le mois.'))
            periode = self.periode_mensuelle
            try:
                periode_label = f"{int(periode):02d}"
            except (TypeError, ValueError):
                raise UserError(_('Mois invalide pour une déclaration mensuelle.')) from None
            declaration_name = _('RD TVA %(periode)s/%(annee)s', periode=periode_label, annee=self.annee)
        else:
            if not self.periode_trimestrielle:
                raise UserError(_('Veuillez renseigner le trimestre.'))
            periode = self.periode_trimestrielle
            declaration_name = _('RD TVA T%(periode)s/%(annee)s', periode=periode, annee=self.annee)

        declaration = self.env['tva.rd.declaration'].create({
            'name': declaration_name,
            'company_id': self.company_id.id,
            'annee': self.annee,
            'periodicite': self.periodicite,
            'periode': periode,
            'regime': self.company_id.l10n_ma_rd_regime_tva or '1',
        })
        declaration.action_generate_lines()

        return {
            'type': 'ir.actions.act_window',
            'name': _('Déclaration Relevé Déductions TVA'),
            'res_model': 'tva.rd.declaration',
            'view_mode': 'form',
            'res_id': declaration.id,
            'target': 'current',
        }
