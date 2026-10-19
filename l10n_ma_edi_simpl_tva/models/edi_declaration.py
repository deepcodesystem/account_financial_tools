import base64
import io
import zipfile
from datetime import date, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


PERIODE_SELECTION = [(str(i), str(i)) for i in range(1, 13)]
TRIMESTRE_SELECTION = [
    ('T1', 'T1 (Jan-Mar)'),
    ('T2', 'T2 (Avr-Jun)'),
    ('T3', 'T3 (Jul-Sep)'),
    ('T4', 'T4 (Oct-Déc)'),
]
DECLARED_PERIODICITE_SELECTION = [('monthly', 'Mensuelle'), ('quarterly', 'Trimestrielle')]
PERIODICITE_SELECTION = DECLARED_PERIODICITE_SELECTION + [('custom', 'Personnalisée')]
QUARTER_TO_PERIODE = {'T1': '1', 'T2': '2', 'T3': '3', 'T4': '4'}
# Taux de TVA par année d'opération, réforme LF 2024 (annexe 1 du cahier des charges EDI RAS V2.0)
VAT_RATES_BY_YEAR = {
    2024: (20.0, 16.0, 13.0, 12.0, 11.0, 10.0, 8.0),
    2025: (20.0, 18.0, 15.0, 12.0, 10.0, 9.0),
    2026: (20.0, 10.0),
}
# Taux antérieurs à la réforme (art. 98 et 99 CGI)
LEGACY_VAT_RATES = (7.0, 10.0, 14.0, 20.0)
ALL_VAT_RATES = tuple(sorted(
    set(LEGACY_VAT_RATES).union(*VAT_RATES_BY_YEAR.values())
))


def get_vat_rates_for_year(year):
    """Taux applicables à une opération de l'année donnée (dernier barème connu au-delà)."""
    if year < min(VAT_RATES_BY_YEAR):
        return LEGACY_VAT_RATES
    return VAT_RATES_BY_YEAR.get(year) or VAT_RATES_BY_YEAR[max(VAT_RATES_BY_YEAR)]


STATE_SELECTION = [('draft', 'Brouillon'), ('validated', 'Validé'), ('exported', 'Exporté')]
# Champs figés dès que la déclaration n'est plus en brouillon
LOCKED_FIELDS = {
    'name', 'company_id', 'annee', 'periodicite', 'periodicite_declaree', 'periode',
    'date_from', 'date_to', 'line_ids',
}


class L10nMaEdiDeclaration(models.AbstractModel):
    """Socle commun des déclarations EDI SIMPL-TVA (relevé des déductions, RAS TVA) :
    période déclarée, périodicité personnalisée, workflow verrouillé, règlements
    fournisseurs de la période et export XML / ZIP."""

    _name = 'l10n.ma.edi.declaration'
    _description = 'Déclaration EDI SIMPL-TVA'

    name = fields.Char(string='Référence', required=True)
    company_id = fields.Many2one(
        'res.company',
        string='Société',
        required=True,
        default=lambda self: self.env.company,
    )
    annee = fields.Char(
        string='Année',
        required=True,
        size=4,
        default=lambda self: str(fields.Date.today().year),
    )
    periodicite = fields.Selection(
        selection='_selection_periodicite',
        string='Périodicité',
        required=True,
        default='monthly',
    )
    periodicite_declaree = fields.Selection(
        selection='_selection_periodicite_declaree',
        string='Périodicité déclarée (DGI)',
        compute='_compute_periodicite_declaree',
        store=True,
        precompute=True,
        readonly=False,
        required=True,
        help="Régime de la déclaration sur SIMPL-TVA (balise <regime>). En mode personnalisé, "
             "c'est la périodicité de la déclaration dans laquelle les opérations sont déposées.",
    )
    periode = fields.Selection(
        selection=lambda self: PERIODE_SELECTION + TRIMESTRE_SELECTION,
        string='Période',
        required=True,
        help="Période déclarée sur SIMPL-TVA (balise <periode>).",
    )
    date_from = fields.Date(
        string='Paiements du',
        help="Périodicité personnalisée : premier jour de paiement repris (ex. paiements oubliés "
             "ou opérations reportées d'une période antérieure).",
    )
    date_to = fields.Date(
        string='Paiements au',
        help="Périodicité personnalisée : dernier jour de paiement repris (inclus). "
             "Ne peut pas dépasser la fin de la période déclarée.",
    )
    state = fields.Selection(
        STATE_SELECTION,
        string='État',
        default='draft',
        required=True,
    )

    # ------------------------------------------------------------------
    # Points d'extension
    # ------------------------------------------------------------------

    @api.model
    def _selection_periodicite(self):
        return PERIODICITE_SELECTION

    @api.model
    def _selection_periodicite_declaree(self):
        return DECLARED_PERIODICITE_SELECTION

    def _get_locked_fields(self):
        return LOCKED_FIELDS

    def _get_default_declared_periodicite(self):
        return 'monthly'

    def _get_edi_max_zip_size(self):
        raise NotImplementedError()

    def _get_export_prefix(self):
        raise NotImplementedError()

    def _get_validation_errors(self):
        raise NotImplementedError()

    def _generate_xml_content(self):
        raise NotImplementedError()

    def _is_settlement_document(self, move):
        """Pièce dont les règlements alimentent la déclaration."""
        raise NotImplementedError()

    def _is_settlement_payment(self, move):
        """Écriture considérée comme un règlement de pièce."""
        return not self._is_settlement_document(move)

    def _before_validate(self):
        return True

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------

    def write(self, vals):
        locked = self._get_locked_fields().intersection(vals)
        if locked and self.filtered(lambda rec: rec.state != 'draft'):
            raise UserError(_(
                "La déclaration n'est plus en brouillon : remettez-la en brouillon pour la modifier."
            ))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_only_draft(self):
        if self.filtered(lambda rec: rec.state != 'draft'):
            raise UserError(_("Seules les déclarations en brouillon peuvent être supprimées."))

    def _check_state(self, allowed_states, action_label):
        for rec in self:
            if rec.state not in allowed_states:
                raise UserError(_(
                    "Action « %(action)s » impossible sur la déclaration %(name)s (état : %(state)s).",
                    action=action_label,
                    name=rec.name,
                    state=dict(rec._fields['state']._description_selection(rec.env))[rec.state],
                ))

    def _check_lines_editable(self):
        if self.filtered(lambda rec: rec.state != 'draft'):
            raise UserError(_(
                "Les lignes d'une déclaration validée ou exportée ne sont pas modifiables : "
                "remettez-la en brouillon."
            ))

    def action_validate(self):
        self._check_state(('draft',), _("Valider"))
        self._before_validate()
        for rec in self:
            errors = rec._get_validation_errors()
            if errors:
                raise UserError(_("La déclaration comporte des anomalies :\n%s") % "\n".join(errors))
            rec.state = 'validated'
        return True

    def action_reset_draft(self):
        self._check_state(('validated', 'exported'), _("Remettre en brouillon"))
        for rec in self:
            rec.state = 'draft'
        return True

    # ------------------------------------------------------------------
    # Période
    # ------------------------------------------------------------------

    @api.depends('periodicite', 'company_id')
    def _compute_periodicite_declaree(self):
        for rec in self:
            if rec.periodicite in ('monthly', 'quarterly'):
                rec.periodicite_declaree = rec.periodicite
            else:
                rec.periodicite_declaree = (
                    rec.periodicite_declaree or rec._get_default_declared_periodicite()
                )

    @api.constrains('annee')
    def _check_annee(self):
        for rec in self:
            if rec.annee and (len(rec.annee) != 4 or not rec.annee.isdigit()):
                raise ValidationError(_("L'année doit contenir exactement 4 chiffres."))

    @api.constrains('periodicite', 'periodicite_declaree', 'periode')
    def _check_periode_periodicite(self):
        for rec in self:
            if not rec.periode:
                continue
            if rec.periodicite_declaree == 'quarterly':
                if rec.periode not in ('T1', 'T2', 'T3', 'T4'):
                    raise ValidationError(_('En mode trimestriel, la période doit être T1, T2, T3 ou T4.'))
            else:
                try:
                    period = int(rec.periode)
                except (TypeError, ValueError):
                    raise ValidationError(_('Période invalide.')) from None
                if period < 1 or period > 12:
                    raise ValidationError(_('En mode mensuel, la période doit être comprise entre 1 et 12.'))

    @api.constrains('periodicite', 'date_from', 'date_to', 'annee', 'periode')
    def _check_custom_dates(self):
        for rec in self.filtered(lambda r: r.periodicite == 'custom'):
            if not rec.date_from or not rec.date_to:
                raise ValidationError(_("Périodicité personnalisée : renseignez les dates de paiement du / au."))
            if rec.date_from > rec.date_to:
                raise ValidationError(_("La date de début doit précéder la date de fin."))
            _start, end_date = rec._get_period_dates()
            if rec.date_to >= end_date:
                raise ValidationError(_(
                    "Les paiements repris ne peuvent pas dépasser la fin de la période déclarée (%(date)s).",
                    date=end_date - timedelta(days=1),
                ))

    def _get_selection_dates(self):
        """Intervalle [début, fin[ des paiements repris dans la déclaration."""
        self.ensure_one()
        if self.periodicite == 'custom':
            return self.date_from, self.date_to + timedelta(days=1)
        return self._get_period_dates()

    def _get_period_dates(self):
        """Intervalle [début, fin[ de la période déclarée (mois ou trimestre DGI)."""
        self.ensure_one()
        try:
            year = int(self.annee)
        except (TypeError, ValueError):
            raise UserError(_('Année invalide.')) from None

        if self.periodicite_declaree == 'quarterly':
            quarter = {'T1': 1, 'T2': 2, 'T3': 3, 'T4': 4}.get(self.periode)
            if not quarter:
                raise UserError(_('Trimestre invalide.'))
            start_month = (quarter - 1) * 3 + 1
            end_month = start_month + 3
            start_date = date(year, start_month, 1)
            end_date = date(year + 1, 1, 1) if end_month > 12 else date(year, end_month, 1)
        else:
            try:
                period = int(self.periode)
            except (TypeError, ValueError):
                raise UserError(_('Période invalide.')) from None
            start_date = date(year, period, 1)
            end_date = date(year + 1, 1, 1) if period == 12 else date(year, period + 1, 1)
        return start_date, end_date

    def _get_periode_xml(self):
        self.ensure_one()
        if self.periodicite_declaree == 'quarterly':
            return QUARTER_TO_PERIODE.get(self.periode, self.periode)
        return self.periode

    # ------------------------------------------------------------------
    # Règlements fournisseurs
    # ------------------------------------------------------------------

    def _get_period_payments(self, start_date, end_date):
        """Règlements fournisseurs de l'intervalle : liste de tuples (pièce, ligne fournisseur
        de la pièce, ligne de règlement, lettrage partiel). Les lettrages entre pièces
        (facture / avoir) et les écarts de change ne sont pas des règlements."""
        self.ensure_one()
        payment_lines = self.env['account.move.line'].search([
            ('company_id', '=', self.company_id.id),
            ('date', '>=', start_date),
            ('date', '<', end_date),
            ('account_id.account_type', '=', 'liability_payable'),
            ('parent_state', '=', 'posted'),
            ('move_id.move_type', 'not in', ('in_invoice', 'in_refund')),
        ], order='date asc, id asc')
        exchange_journal = self.company_id.currency_exchange_journal_id
        if exchange_journal:
            payment_lines = payment_lines.filtered(lambda l: l.journal_id != exchange_journal)

        payments = []
        for payment_line in payment_lines:
            if not self._is_settlement_payment(payment_line.move_id):
                continue
            for partial in payment_line.matched_debit_ids | payment_line.matched_credit_ids:
                document_line = (
                    partial.debit_move_id if partial.credit_move_id == payment_line
                    else partial.credit_move_id
                )
                document = document_line.move_id
                if self._is_settlement_document(document):
                    payments.append((document, document_line, payment_line, partial))
        return payments

    def _get_paid_amount_currency(self, document_line, partial):
        if partial.debit_move_id == document_line:
            return abs(partial.debit_amount_currency)
        return abs(partial.credit_amount_currency)

    def _get_payable_total_currency(self, document):
        return abs(sum(document.line_ids.filtered(
            lambda l: l.account_id.account_type == 'liability_payable'
        ).mapped('amount_currency')))

    def _get_paid_ratio(self, document, document_line, partial):
        """Part de la pièce couverte par le lettrage, calculée en devise de la pièce."""
        total = self._get_payable_total_currency(document)
        if not total:
            return 0.0
        return min(self._get_paid_amount_currency(document_line, partial) / total, 1.0)

    def _get_settlements_declared_elsewhere(self, documents):
        """{(pièce, règlement): déclaration} déjà repris dans une autre déclaration de la société."""
        self.ensure_one()
        if not documents:
            return {}
        other_lines = self.env[self.line_ids._name].search([
            ('company_id', '=', self.company_id.id),
            ('declaration_id', '!=', self.id),
            ('move_id', 'in', documents.ids),
            ('payment_move_id', '!=', False),
        ])
        return {(line.move_id.id, line.payment_move_id.id): line.declaration_id for line in other_lines}

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _get_export_filename_base(self):
        self.ensure_one()
        try:
            year = int(self.annee)
            month = int(self._get_periode_xml())
        except (TypeError, ValueError):
            raise UserError(_('Année ou période invalide pour l\'export.')) from None
        return f"{self._get_export_prefix()}_{self.company_id.id}_{year}_{month:02d}"

    def _create_attachment(self, filename, content, mimetype):
        return self.env['ir.attachment'].create({
            'name': filename,
            'datas': base64.b64encode(content),
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': mimetype,
        })

    def _download_action(self, attachment):
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    def action_export_xml(self):
        self.ensure_one()
        self._check_state(('validated', 'exported'), _("Exporter"))
        xml_content = self._generate_xml_content()
        filename = f"{self._get_export_filename_base()}.xml"
        attachment = self._create_attachment(filename, xml_content, 'application/xml')
        self.state = 'exported'
        return self._download_action(attachment)

    def action_export_zip(self):
        self.ensure_one()
        self._check_state(('validated', 'exported'), _("Exporter"))
        xml_content = self._generate_xml_content()
        base_name = self._get_export_filename_base()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{base_name}.xml", xml_content)
        max_size = self._get_edi_max_zip_size()
        if len(buffer.getvalue()) > max_size:
            raise UserError(_(
                "Le fichier compressé fait %(size).0f Ko : SIMPL-TVA refuse les fichiers de plus de "
                "%(max).0f Ko (cahier des charges §2.3.1).",
                size=len(buffer.getvalue()) / 1024, max=max_size / 1024,
            ))
        attachment = self._create_attachment(f"{base_name}.zip", buffer.getvalue(), 'application/zip')
        self.state = 'exported'
        return self._download_action(attachment)


class L10nMaEdiDeclarationLine(models.AbstractModel):
    """Socle commun des lignes : modifiables uniquement en brouillon."""

    _name = 'l10n.ma.edi.declaration.line'
    _description = 'Ligne de déclaration EDI SIMPL-TVA'

    def _declaration_model(self):
        return self.env[self._fields['declaration_id'].comodel_name]

    @api.model_create_multi
    def create(self, vals_list):
        self._declaration_model().browse(
            {vals['declaration_id'] for vals in vals_list if vals.get('declaration_id')}
        )._check_lines_editable()
        return super().create(vals_list)

    def write(self, vals):
        self.declaration_id._check_lines_editable()
        if 'declaration_id' in vals:
            self._declaration_model().browse(vals['declaration_id'])._check_lines_editable()
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_only_draft(self):
        self.declaration_id._check_lines_editable()
