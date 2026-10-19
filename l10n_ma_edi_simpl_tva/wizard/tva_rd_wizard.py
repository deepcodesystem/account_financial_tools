from odoo import _, fields, models
from odoo.exceptions import UserError

from ..models.edi_declaration import (
    DECLARED_PERIODICITE_SELECTION,
    PERIODICITE_SELECTION,
    PERIODE_SELECTION,
    TRIMESTRE_SELECTION,
)


class TvaRdWizard(models.TransientModel):
    _name = 'tva.rd.wizard'
    _description = 'Assistant Déclaration EDI SIMPL-TVA'

    declaration_type = fields.Selection(
        [('rd', 'Relevé des déductions'), ('ras', 'TVA retenue à la source')],
        string='Déclaration',
        required=True,
        default='rd',
    )

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
    periodicite_declaree = fields.Selection(
        DECLARED_PERIODICITE_SELECTION,
        string='Périodicité déclarée (DGI)',
        default=lambda self: self.env.company.l10n_ma_rd_periodicite or 'monthly',
        help="Périodicité de la déclaration SIMPL-TVA dans laquelle les déductions sont déposées.",
    )
    date_from = fields.Date(string='Paiements du')
    date_to = fields.Date(string='Paiements au')
    periode_mensuelle = fields.Selection(
        PERIODE_SELECTION,
        string='Mois',
        default=lambda self: str(fields.Date.today().month),
    )
    periode_trimestrielle = fields.Selection(
        TRIMESTRE_SELECTION,
        string='Trimestre',
        default='T1',
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

        is_ras = self.declaration_type == 'ras'
        if is_ras and self.periodicite == 'quarterly':
            raise UserError(_("La déclaration de la TVA retenue à la source est mensuelle."))
        declared = self.periodicite if self.periodicite != 'custom' else self.periodicite_declaree
        if is_ras:
            declared = 'monthly'
        if not declared:
            raise UserError(_('Veuillez renseigner la périodicité déclarée.'))
        if self.periodicite == 'custom' and not (self.date_from and self.date_to):
            raise UserError(_('Veuillez renseigner les dates de paiement du / au.'))

        if declared == 'monthly':
            if not self.periode_mensuelle:
                raise UserError(_('Veuillez renseigner le mois.'))
            periode = self.periode_mensuelle
            try:
                periode_label = f"{int(periode):02d}"
            except (TypeError, ValueError):
                raise UserError(_('Mois invalide pour une déclaration mensuelle.')) from None
            declaration_name = _(
                '%(prefix)s %(periode)s/%(annee)s',
                prefix='RAS TVA' if is_ras else 'RD TVA', periode=periode_label, annee=self.annee,
            )
        else:
            if not self.periode_trimestrielle:
                raise UserError(_('Veuillez renseigner le trimestre.'))
            periode = self.periode_trimestrielle
            declaration_name = _('RD TVA %(periode)s/%(annee)s', periode=periode, annee=self.annee)

        if self.periodicite == 'custom':
            declaration_name = _(
                '%(name)s (paiements du %(date_from)s au %(date_to)s)',
                name=declaration_name,
                date_from=self.date_from.strftime('%d/%m/%Y'),
                date_to=self.date_to.strftime('%d/%m/%Y'),
            )

        model = 'tva.ras.declaration' if is_ras else 'tva.rd.declaration'
        existing = self.env[model].search([
            ('company_id', '=', self.company_id.id),
            ('annee', '=', self.annee),
            ('periode', '=', periode),
        ], limit=1)
        if existing:
            raise UserError(_(
                "La déclaration %(name)s existe déjà pour cette période : ouvrez-la et "
                "régénérez ses lignes (en brouillon) plutôt que d'en créer une nouvelle.",
                name=existing.name,
            ))

        vals = {
            'name': declaration_name,
            'company_id': self.company_id.id,
            'annee': self.annee,
            'periodicite': self.periodicite,
            'periodicite_declaree': declared,
            'periode': periode,
            'date_from': self.date_from if self.periodicite == 'custom' else False,
            'date_to': self.date_to if self.periodicite == 'custom' else False,
        }
        if not is_ras:
            vals['regime_comptabilite'] = self.company_id.l10n_ma_rd_regime_tva or '1'
        declaration = self.env[model].create(vals)
        declaration.action_generate_lines()

        return {
            'type': 'ir.actions.act_window',
            'name': declaration_name,
            'res_model': model,
            'view_mode': 'form',
            'res_id': declaration.id,
            'target': 'current',
        }
