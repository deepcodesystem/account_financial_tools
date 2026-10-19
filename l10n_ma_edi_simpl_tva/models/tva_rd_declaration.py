import re
from datetime import date

from dateutil.relativedelta import relativedelta

from lxml import etree

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.mail import html2plaintext

from .edi_declaration import (  # noqa: F401 (réexportés pour l'assistant)
    ALL_VAT_RATES,
    DECLARED_PERIODICITE_SELECTION,
    LOCKED_FIELDS as BASE_LOCKED_FIELDS,
    PERIODE_SELECTION,
    PERIODICITE_SELECTION,
    TRIMESTRE_SELECTION,
)


REGIME_DECLARATION_SELECTION = [
    ('1', 'Mensuel'),
    ('2', 'Trimestriel'),
]
REGIME_COMPTABILITE_SELECTION = [('1', 'Débit'), ('2', 'Encaissement')]
MODE_PAIEMENT_SELECTION = [
    ('1', 'Espèces'),
    ('2', 'Chèque'),
    ('3', 'Prélèvement'),
    ('4', 'Virement'),
    ('5', 'Effet'),
    ('6', 'Compensation'),
    ('7', 'Autres'),
]
# Art. 101-3° CGI : le droit à déduction s'exerce dans l'année qui suit le mois du paiement
DEDUCTION_DELAY = relativedelta(years=1)
DEFAULT_MODE_PAIEMENT = '7'
CASH_MODE_PAIEMENT = '1'
# Cahier des charges §2.3.1 : 1 Mo maximum pour le fichier compressé
MAX_EDI_ZIP_SIZE = 1024 * 1024
MODE_PAIEMENT_KEYWORDS = [
    ('2', ('check', 'chèque', 'cheque', 'pdc')),
    ('5', ('effet', 'lcn', 'traite', 'lettre de change', 'billet à ordre', 'billet a ordre')),
    ('3', ('sepa_dd', 'direct_debit', 'direct debit', 'prélèvement', 'prelevement')),
    ('6', ('compensation', 'netting')),
]
# Taux légaux, y compris les taux transitoires de la réforme 2024-2026 : une facture 2024
# à 16 % payée en 2026 reste déductible à son taux d'origine
LEGAL_VAT_RATES = ALL_VAT_RATES
# Champs figés dès que la déclaration n'est plus en brouillon
LOCKED_FIELDS = BASE_LOCKED_FIELDS | {'regime_comptabilite'}
RD_TAGS_ORDER = [
    'ord', 'num', 'des', 'mht', 'tva', 'ttc', 'refF',
    'tx', 'prorata', 'mp', 'dpai', 'dfac',
]
REFF_TAGS_ORDER = ['if', 'nom', 'ice']
AMOUNT_RE = re.compile(r'^-?\d+\.\d{2}$')
RATE_RE = re.compile(r'^\d+\.\d{2}$')
INT_RE = re.compile(r'^\d+$')
PRORATA_RE = re.compile(r'^\d+(\.\d+)?$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


class TvaRdDeclaration(models.Model):
    _name = 'tva.rd.declaration'
    _inherit = 'l10n.ma.edi.declaration'
    _description = 'Déclaration Relevé de Déductions TVA'
    _rec_name = 'name'
    _order = 'annee desc, id desc'

    _sql_constraints = [
        ('company_annee_periode_uniq', 'unique(company_id, annee, periode)',
         'Une déclaration existe déjà pour cette société, cette année et cette période.'),
    ]

    regime = fields.Selection(
        REGIME_DECLARATION_SELECTION,
        string='Régime de déclaration',
        compute='_compute_regime',
        store=True,
        readonly=True,
    )
    regime_comptabilite = fields.Selection(
        REGIME_COMPTABILITE_SELECTION,
        string='Régime de comptabilisation',
        required=True,
        default=lambda self: self.env.company.l10n_ma_rd_regime_tva or '1',
        help="Information uniquement : quel que soit le régime, le relevé reprend "
             "les factures payées pendant la période (art. 101-2° CGI).",
    )
    line_ids = fields.One2many('tva.rd.declaration.line', 'declaration_id', string='Lignes')
    line_count = fields.Integer(string='Nb Lignes', compute='_compute_line_count')
    warning_message = fields.Text(string='Alertes', compute='_compute_warning_message')

    def _get_locked_fields(self):
        return LOCKED_FIELDS

    def _get_default_declared_periodicite(self):
        return self.company_id.l10n_ma_rd_periodicite or 'monthly'

    def _get_edi_max_zip_size(self):
        return MAX_EDI_ZIP_SIZE

    def _get_export_prefix(self):
        return 'RD_TVA'

    def _is_settlement_document(self, move):
        return self._is_deduction_document(move)

    def _before_validate(self):
        return self._refresh_supplier_data()

    @api.depends('periodicite_declaree')
    def _compute_regime(self):
        for rec in self:
            rec.regime = '2' if rec.periodicite_declaree == 'quarterly' else '1'

    @api.depends(
        'company_id.l10n_ma_rd_cash_daily_limit', 'company_id.l10n_ma_rd_cash_monthly_limit',
        'line_ids.mode_paiement', 'line_ids.montant_ttc', 'line_ids.date_paiement',
        'line_ids.partner_id', 'line_ids.if_fournisseur', 'line_ids.nom_fournisseur',
    )
    def _compute_warning_message(self):
        for rec in self:
            rec.warning_message = '\n'.join(rec._get_cash_limit_warnings()[1]) or False

    def _get_cash_limit_warnings(self):
        """Règlements en espèces dépassant les plafonds de l'art. 106 CGI (non bloquant).

        Retourne (lignes concernées, messages). Cumul TTC par fournisseur, par jour et par mois.
        """
        self.ensure_one()
        flagged = self.env['tva.rd.declaration.line']
        messages = []
        daily_limit = self.company_id.l10n_ma_rd_cash_daily_limit
        monthly_limit = self.company_id.l10n_ma_rd_cash_monthly_limit
        cash_lines = self.line_ids.filtered(
            lambda l: l.mode_paiement == CASH_MODE_PAIEMENT and l.date_paiement
        )
        if not cash_lines or not (daily_limit or monthly_limit):
            return flagged, messages

        by_day, by_month = {}, {}
        for line in cash_lines:
            supplier = line.partner_id.id or (line.if_fournisseur, line.nom_fournisseur)
            by_day.setdefault((supplier, line.date_paiement), self.env['tva.rd.declaration.line'])
            by_day[(supplier, line.date_paiement)] |= line
            month = line.date_paiement.strftime('%m/%Y')
            by_month.setdefault((supplier, month), self.env['tva.rd.declaration.line'])
            by_month[(supplier, month)] |= line

        for groups, limit, label in (
            (by_day, daily_limit, _("par jour")),
            (by_month, monthly_limit, _("par mois")),
        ):
            if not limit:
                continue
            for (_supplier, period), lines in groups.items():
                total = sum(lines.mapped('montant_ttc'))
                exceeded = total >= limit if groups is by_day else total > limit
                if exceeded:
                    flagged |= lines
                    messages.append(_(
                        "%(supplier)s : %(total).2f TTC réglés en espèces le %(period)s (plafond %(label)s "
                        "%(limit).2f) — TVA non déductible (art. 106 CGI), lignes %(ords)s.",
                        supplier=lines[0].nom_fournisseur, total=total, period=period, label=label,
                        limit=limit, ords=', '.join(str(o) for o in sorted(lines.mapped('ord'))),
                    ))
        return flagged, messages

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    def _get_deduction_min_date(self):
        """Paiement le plus ancien encore déductible dans la période déclarée (art. 101-3° CGI)."""
        start_date, _end = self._get_period_dates()
        return start_date - DEDUCTION_DELAY

    def _get_mode_paiement(self, payment_move):
        """Code DGI du mode de paiement d'une écriture de règlement, par ordre de priorité :
        mode configuré sur la méthode de paiement, caisse, compensation, mots-clés de la
        méthode, mode par défaut du journal, banque (virement), autres."""
        payment = payment_move.origin_payment_id
        method_line = payment.payment_method_line_id
        if method_line.l10n_ma_rd_mode_paiement:
            return method_line.l10n_ma_rd_mode_paiement
        journal = payment_move.journal_id
        if journal.type == 'cash':
            return CASH_MODE_PAIEMENT
        if self._is_compensation_move(payment_move):
            return '6'

        method_tokens = ' '.join(filter(None, [
            method_line.payment_method_id.code or '',
            method_line.name or '',
        ])).lower()
        for mode, keywords in MODE_PAIEMENT_KEYWORDS:
            if any(keyword in method_tokens for keyword in keywords):
                return mode

        if journal.l10n_ma_rd_mode_paiement:
            return journal.l10n_ma_rd_mode_paiement
        if journal.type == 'bank':
            return '4'
        return DEFAULT_MODE_PAIEMENT

    def _is_compensation_move(self, move):
        """Règlement par compensation : facture / avoir client, ou écriture diverse soldant
        une dette fournisseur contre une créance client."""
        if move.move_type in ('out_invoice', 'out_refund'):
            return True
        if move.move_type != 'entry' or move.journal_id.type in ('bank', 'cash'):
            return False
        account_types = set(move.line_ids.mapped('account_id.account_type'))
        return {'liability_payable', 'asset_receivable'} <= account_types

    def _is_deductible_tax_line(self, line):
        """Ligne de TVA d'achat récupérable (exclut la part non déductible et l'autoliquidation négative)."""
        tax = line.tax_line_id
        repartition = line.tax_repartition_line_id
        return bool(
            tax
            and tax.type_tax_use == 'purchase'
            and tax.amount_type in ('percent', 'division')
            and tax.amount > 0
            and repartition.factor_percent > 0
            and repartition.use_in_tax_closing
        )

    def _is_deduction_document(self, move):
        """Pièce porteuse de TVA déductible : facture / avoir fournisseur, ou écriture
        diverse (frais bancaires, notes de frais...) comportant de la TVA d'achat."""
        if move.state != 'posted':
            return False
        if move.move_type in ('in_invoice', 'in_refund'):
            return True
        if move.move_type != 'entry' or move.tax_cash_basis_origin_move_id:
            return False
        exchange_journal = move.company_id.currency_exchange_journal_id
        if exchange_journal and move.journal_id == exchange_journal:
            return False
        return any(self._is_deductible_tax_line(line) for line in move.line_ids)

    def _get_direct_paid_entries(self, start_date, end_date):
        """Écritures diverses de la période avec TVA d'achat et sans compte fournisseur :
        réglées au moment de leur comptabilisation (ex. frais bancaires prélevés sur relevé)."""
        self.ensure_one()
        moves = self.env['account.move'].search([
            ('company_id', '=', self.company_id.id),
            ('move_type', '=', 'entry'),
            ('state', '=', 'posted'),
            ('date', '>=', start_date),
            ('date', '<', end_date),
            ('tax_cash_basis_origin_move_id', '=', False),
            ('line_ids.tax_line_id.type_tax_use', '=', 'purchase'),
        ], order='date asc, id asc')
        return moves.filtered(lambda m: self._is_deduction_document(m) and not any(
            line.account_id.account_type == 'liability_payable' for line in m.line_ids
        ))

    def _get_direct_mode_paiement(self, move):
        journal = move.journal_id
        if journal.l10n_ma_rd_mode_paiement:
            return journal.l10n_ma_rd_mode_paiement
        if journal.type == 'cash':
            return CASH_MODE_PAIEMENT
        if journal.type == 'bank':
            return '3'
        return DEFAULT_MODE_PAIEMENT

    def _get_amounts_by_rate(self, move):
        """{taux: [HT, TVA]} de la TVA déductible comptabilisée, en devise société.

        La TVA est reprise des lignes de taxe de la pièce (et non recalculée) ; la base
        est la somme des lignes portant cette taxe. Montants signés : négatifs pour les avoirs.
        Les lignes sans TVA déductible (0 %, exonérées, non récupérables) sont ignorées.
        """
        tax_lines = move.line_ids.filtered(self._is_deductible_tax_line)
        grouped = {}
        for tax in tax_lines.tax_line_id:
            amounts = grouped.setdefault(round(tax.amount, 2), [0.0, 0.0])
            amounts[0] += sum(move.line_ids.filtered(lambda l: tax in l.tax_ids).mapped('balance'))
            amounts[1] += sum(tax_lines.filtered(lambda l: l.tax_line_id == tax).mapped('balance'))
        currency = move.company_id.currency_id
        return {rate: amounts for rate, amounts in grouped.items() if not currency.is_zero(amounts[1])}

    def _get_document_description(self, move):
        base_lines = move.line_ids.filtered(
            lambda l: not l.tax_line_id
            and l.display_type == 'product'
            and (move.move_type != 'entry' or l.tax_ids)
        )
        description_parts = []
        for base_line in base_lines:
            label = html2plaintext(base_line.name or '').strip().replace('\n', ' ')
            if label and label not in description_parts:
                description_parts.append(label)
        description = ' / '.join(description_parts)[:255]
        if not description:
            narration_raw = move.narration or ''
            description = (
                html2plaintext(narration_raw).strip().replace('\n', ' ') if narration_raw
                else (move.ref or move.name or '')
            )[:255]
        return description

    def _get_document_partner(self, move):
        partner = move.partner_id or move.line_ids.partner_id[:1]
        return partner.commercial_partner_id

    def _get_document_values(self, move):
        partner = self._get_document_partner(move)
        supplier_values = self.env['tva.rd.declaration.line']._get_partner_supplier_values(partner)
        if not supplier_values['nom_fournisseur'] and move.journal_id.type == 'bank':
            supplier_values['nom_fournisseur'] = move.journal_id.bank_id.name or ''
        num = move.ref or move.statement_line_id.payment_ref or move.name or ''
        return {
            'partner_id': partner.id,
            'amounts': self._get_amounts_by_rate(move),
            'num_facture': num[:50],
            'description': self._get_document_description(move),
            **supplier_values,
            'date_facture': move.invoice_date or move.date,
        }

    def _refresh_supplier_data(self, partners=None):
        """Reprend nom, IF et ICE depuis les fiches fournisseurs (brouillons uniquement).

        Seules les valeurs renseignées sur la fiche remplacent celles de la ligne :
        une saisie manuelle sur la ligne n'est pas effacée par une fiche incomplète.
        """
        lines = self.filtered(lambda rec: rec.state == 'draft').line_ids.filtered('partner_id')
        if partners is not None:
            lines = lines.filtered(lambda l: l.partner_id.commercial_partner_id in partners.commercial_partner_id)
        line_model = self.env['tva.rd.declaration.line']
        for line in lines:
            vals = {
                field: value
                for field, value in line_model._get_partner_supplier_values(line.partner_id).items()
                if value and value != line[field]
            }
            if vals:
                line.write(vals)
        return True

    def action_refresh_suppliers(self):
        self._check_state(('draft',), _("Actualiser les fournisseurs"))
        return self._refresh_supplier_data()

    def action_generate_lines(self):
        self._check_state(('draft',), _("Générer les lignes"))
        for rec in self:
            if not rec.annee or not rec.periode:
                raise UserError(_('Veuillez renseigner l\'année et la période.'))

            start_date, end_date = rec._get_selection_dates()
            # (document, date de paiement, part payée, mode, pièce de règlement, clé de tri)
            settlements = []
            for document, document_line, payment_line, partial in rec._get_period_payments(start_date, end_date):
                settlements.append((
                    document,
                    payment_line.date,
                    rec._get_paid_ratio(document, document_line, partial),
                    rec._get_mode_paiement(payment_line.move_id),
                    payment_line.move_id,
                    partial.id,
                ))
            for move in rec._get_direct_paid_entries(start_date, end_date):
                settlements.append((move, move.date, 1.0, rec._get_direct_mode_paiement(move), move, 0))
            declared = rec._get_settlements_declared_elsewhere(
                self.env['account.move'].union(*(settlement[0] for settlement in settlements))
            )
            settlements = [
                settlement for settlement in settlements
                if (settlement[0].id, settlement[4].id) not in declared
            ]
            settlements.sort(key=lambda s: (
                s[1], s[0].invoice_date or s[0].date or date.min, s[0].id, s[5],
            ))

            lines_vals = [(5, 0, 0)]
            seq = 1
            document_cache = {}
            for document, payment_date, ratio, mode_paiement, payment_move, _key in settlements:
                if document.id not in document_cache:
                    document_cache[document.id] = rec._get_document_values(document)
                cached = document_cache[document.id]

                for rate in sorted(cached['amounts']):
                    montant_ht, montant_tva = cached['amounts'][rate]
                    lines_vals.append((0, 0, {
                        'move_id': document.id,
                        'partner_id': cached['partner_id'],
                        'payment_move_id': payment_move.id,
                        'ord': seq,
                        'num_facture': cached['num_facture'],
                        'description': cached['description'],
                        'montant_ht': round(montant_ht * ratio, 2),
                        'montant_tva': round(montant_tva * ratio, 2),
                        'if_fournisseur': cached['if_fournisseur'],
                        'nom_fournisseur': cached['nom_fournisseur'],
                        'ice_fournisseur': cached['ice_fournisseur'],
                        'taux_tva': rate,
                        'prorata': rec.company_id.l10n_ma_rd_prorata or 100.0,
                        'mode_paiement': mode_paiement,
                        'date_paiement': payment_date,
                        'date_facture': cached['date_facture'],
                    }))
                    seq += 1
            rec.line_ids = lines_vals
        return True

    def _get_validation_errors(self):
        """Contrôles bloquants avant validation et avant chaque export."""
        self.ensure_one()
        errors = []
        identifiant = self.company_id._l10n_ma_get_if()
        if not identifiant:
            errors.append(_("L'identifiant fiscal de la société est obligatoire (N° TVA de la société)."))
        elif not identifiant.isdigit() or len(identifiant) > 8:
            errors.append(_("L'identifiant fiscal de la société doit contenir uniquement des chiffres (8 maximum)."))
        if not self.line_ids:
            errors.append(_("La déclaration ne contient aucune ligne."))

        start_date, end_date = self._get_selection_dates()
        min_date = self._get_deduction_min_date()
        declared = self._get_settlements_declared_elsewhere(self.line_ids.move_id)
        ords = self.line_ids.mapped('ord')
        duplicated_ords = sorted({o for o in ords if ords.count(o) > 1})
        if duplicated_ords:
            errors.append(_(
                "Numéros d'ordre en double : %(ords)s.",
                ords=', '.join(str(o) for o in duplicated_ords),
            ))
        for line in self.line_ids.sorted('ord'):
            label = _("Ligne %(ord)s", ord=line.ord)
            if line.ord <= 0:
                errors.append(_("%(label)s : le numéro d'ordre doit être strictement positif.", label=label))
            if not line.num_facture:
                errors.append(_("%(label)s : numéro de facture manquant.", label=label))
            if not line.description:
                errors.append(_("%(label)s : désignation manquante.", label=label))
            if not line.nom_fournisseur:
                errors.append(_("%(label)s : nom du fournisseur manquant.", label=label))

            identifiers_required = line._is_identifiers_required()
            if not line.if_fournisseur:
                if identifiers_required:
                    errors.append(_("%(label)s : identifiant fiscal (IF) du fournisseur manquant.", label=label))
            elif not line.if_fournisseur.isdigit() or len(line.if_fournisseur) > 8:
                errors.append(_("%(label)s : l'IF du fournisseur doit contenir uniquement des chiffres (8 maximum).", label=label))
            if not line.ice_fournisseur:
                if identifiers_required:
                    errors.append(_("%(label)s : ICE du fournisseur manquant.", label=label))
            elif not line.ice_fournisseur.isdigit() or len(line.ice_fournisseur) != 15:
                errors.append(_("%(label)s : l'ICE du fournisseur doit contenir 15 chiffres.", label=label))

            if round(line.taux_tva, 2) not in LEGAL_VAT_RATES:
                errors.append(_(
                    "%(label)s : taux de TVA %(rate)s %% non autorisé (taux légaux : %(rates)s).",
                    label=label, rate=f"{line.taux_tva:g}",
                    rates=', '.join(f"{r:g}" for r in LEGAL_VAT_RATES),
                ))
            if not 0.0 < line.prorata <= 100.0:
                errors.append(_("%(label)s : le prorata doit être compris entre 0 (exclu) et 100.", label=label))
            if not line.mode_paiement:
                errors.append(_("%(label)s : mode de paiement manquant.", label=label))
            if not line.date_facture:
                errors.append(_("%(label)s : date de facture manquante.", label=label))
            if not line.date_paiement:
                errors.append(_("%(label)s : date de paiement manquante.", label=label))
            elif not start_date <= line.date_paiement < end_date:
                errors.append(_(
                    "%(label)s : la date de paiement %(date)s est hors de la période déclarée.",
                    label=label, date=line.date_paiement,
                ))
            elif line.date_paiement < min_date:
                errors.append(_(
                    "%(label)s : paiement du %(date)s, délai de déduction d'un an dépassé "
                    "(paiements antérieurs au %(min)s non déductibles, art. 101-3° CGI).",
                    label=label, date=line.date_paiement, min=min_date,
                ))
            other = declared.get((line.move_id.id, line.payment_move_id.id))
            if line.payment_move_id and other:
                errors.append(_(
                    "%(label)s : ce paiement est déjà déclaré dans %(declaration)s (double déduction).",
                    label=label, declaration=other.name,
                ))
            if line.date_paiement and line.date_facture and line.date_paiement < line.date_facture:
                errors.append(_("%(label)s : la date de paiement est antérieure à la date de facture.", label=label))
        return errors

    def _check_xml_conformity(self, root):
        errors = []
        if root.tag != 'DeclarationReleveDeduction':
            errors.append(_("Balise racine inattendue : %(tag)s", tag=root.tag))
        for tag in ('identifiantFiscal', 'annee', 'periode', 'regime'):
            if root.find(tag) is None:
                errors.append(_("Balise obligatoire manquante : <%(tag)s>", tag=tag))

        identifiant = (root.findtext('identifiantFiscal') or '').strip()
        if identifiant and not identifiant.isdigit():
            errors.append(_("L'identifiant fiscal doit être numérique."))
        annee = (root.findtext('annee') or '').strip()
        if annee and not re.match(r'^\d{4}$', annee):
            errors.append(_("L'année doit être au format AAAA."))
        regime = (root.findtext('regime') or '').strip()
        periode = (root.findtext('periode') or '').strip()
        if regime not in ('1', '2', '3', '4'):
            errors.append(_("Le régime doit valoir 1, 2, 3 ou 4."))
        elif not INT_RE.match(periode or ''):
            errors.append(_("La période doit être un entier."))
        else:
            periode_int = int(periode)
            if regime == '1' and not 1 <= periode_int <= 12:
                errors.append(_("Régime mensuel (1) : la période doit être comprise entre 1 et 12."))
            elif regime == '2' and not 1 <= periode_int <= 4:
                errors.append(_("Régime trimestriel (2) : la période doit être comprise entre 1 et 4."))

        rd_nodes = root.findall('./releveDeductions/rd')
        if not rd_nodes:
            errors.append(_("Le relevé ne contient aucune ligne <rd>."))
        for index, rd in enumerate(rd_nodes, start=1):
            label = _("Ligne %(num)s", num=index)
            tags = [child.tag for child in rd]
            if tags != RD_TAGS_ORDER:
                errors.append(_(
                    "%(label)s : structure des balises inattendue : %(tags)s",
                    label=label, tags=', '.join(tags),
                ))
            if not INT_RE.match((rd.findtext('ord') or '').strip() or ''):
                errors.append(_("%(label)s : numéro d'ordre invalide.", label=label))
            if not (rd.findtext('num') or '').strip():
                errors.append(_("%(label)s : numéro de facture manquant.", label=label))
            if not (rd.findtext('des') or '').strip():
                errors.append(_("%(label)s : désignation manquante.", label=label))
            for tag in ('mht', 'tva', 'ttc'):
                value = (rd.findtext(tag) or '').strip()
                if not AMOUNT_RE.match(value or ''):
                    errors.append(_("%(label)s : montant <%(tag)s> invalide.", label=label, tag=tag))
            tx = (rd.findtext('tx') or '').strip()
            if not RATE_RE.match(tx or ''):
                errors.append(_("%(label)s : taux de taxe invalide.", label=label))
            if not PRORATA_RE.match((rd.findtext('prorata') or '').strip() or ''):
                errors.append(_("%(label)s : prorata invalide.", label=label))
            try:
                mht = float((rd.findtext('mht') or '').strip() or 0.0)
                tva = float((rd.findtext('tva') or '').strip() or 0.0)
                ttc = float((rd.findtext('ttc') or '').strip() or 0.0)
            except ValueError:
                pass
            else:
                if abs(mht + tva - ttc) > 0.01:
                    errors.append(_("%(label)s : TTC doit être égal à HT + TVA.", label=label))
            ref_f = rd.find('refF')
            if ref_f is None:
                errors.append(_("%(label)s : balise <refF> manquante.", label=label))
            else:
                if [child.tag for child in ref_f] != REFF_TAGS_ORDER:
                    errors.append(_("%(label)s : structure de <refF> inattendue.", label=label))
                if not (ref_f.findtext('nom') or '').strip():
                    errors.append(_("%(label)s : nom du fournisseur manquant.", label=label))
                if_value = (ref_f.findtext('if') or '').strip()
                if if_value and not if_value.isdigit():
                    errors.append(_("%(label)s : identifiant fiscal fournisseur invalide.", label=label))
                ice_value = (ref_f.findtext('ice') or '').strip()
                if ice_value and not re.match(r'^\d{15}$', ice_value):
                    errors.append(_("%(label)s : ICE fournisseur invalide (15 chiffres).", label=label))
            mp = rd.find('mp')
            mp_id = (mp.findtext('id') or '').strip() if mp is not None else ''
            if mp_id not in ('1', '2', '3', '4', '5', '6', '7'):
                errors.append(_("%(label)s : mode de paiement invalide.", label=label))
            for tag in ('dpai', 'dfac'):
                value = (rd.findtext(tag) or '').strip()
                if value and not DATE_RE.match(value):
                    errors.append(_("%(label)s : date <%(tag)s> invalide.", label=label, tag=tag))
        return errors

    def _generate_xml_content(self):
        self.ensure_one()
        # Les contrôles sont relancés à chaque export, même après validation
        errors = self._get_validation_errors()
        if errors:
            raise UserError(_("La déclaration comporte des anomalies :\n%s") % "\n".join(errors[:30]))

        root = etree.Element('DeclarationReleveDeduction')
        etree.SubElement(root, 'identifiantFiscal').text = self.company_id._l10n_ma_get_if()
        etree.SubElement(root, 'annee').text = self.annee
        etree.SubElement(root, 'periode').text = self._get_periode_xml()
        etree.SubElement(root, 'regime').text = self.regime or ('2' if self.periodicite == 'quarterly' else '1')

        releve = etree.SubElement(root, 'releveDeductions')
        for line in self.line_ids.sorted('ord'):
            rd = etree.SubElement(releve, 'rd')
            etree.SubElement(rd, 'ord').text = str(line.ord or 0)
            etree.SubElement(rd, 'num').text = line.num_facture or ''
            etree.SubElement(rd, 'des').text = line.description or ''
            etree.SubElement(rd, 'mht').text = f"{(line.montant_ht or 0.0):.2f}"
            etree.SubElement(rd, 'tva').text = f"{(line.montant_tva or 0.0):.2f}"
            etree.SubElement(rd, 'ttc').text = f"{(line.montant_ttc or 0.0):.2f}"
            ref_f = etree.SubElement(rd, 'refF')
            etree.SubElement(ref_f, 'if').text = line.if_fournisseur or ''
            etree.SubElement(ref_f, 'nom').text = line.nom_fournisseur or ''
            etree.SubElement(ref_f, 'ice').text = line.ice_fournisseur or ''
            etree.SubElement(rd, 'tx').text = f"{(line.taux_tva or 0.0):.2f}"
            etree.SubElement(rd, 'prorata').text = '%g' % (line.prorata or 0.0)
            mp = etree.SubElement(rd, 'mp')
            etree.SubElement(mp, 'id').text = line.mode_paiement or DEFAULT_MODE_PAIEMENT
            etree.SubElement(rd, 'dpai').text = str(line.date_paiement) if line.date_paiement else ''
            etree.SubElement(rd, 'dfac').text = str(line.date_facture) if line.date_facture else ''

        errors = self._check_xml_conformity(root)
        if errors:
            raise UserError(_("Le fichier EDI n'est pas conforme :\n%s") % "\n".join(errors[:30]))

        return etree.tostring(root, xml_declaration=True, encoding='UTF-8', pretty_print=True)

class TvaRdDeclarationLine(models.Model):
    _name = 'tva.rd.declaration.line'
    _inherit = 'l10n.ma.edi.declaration.line'
    _description = 'Ligne Relevé de Déductions TVA'
    _order = 'ord asc, id asc'

    declaration_id = fields.Many2one(
        'tva.rd.declaration',
        string='Déclaration',
        required=True,
        ondelete='cascade',
    )
    company_id = fields.Many2one(
        related='declaration_id.company_id',
        store=True,
        index=True,
        readonly=True,
    )
    move_id = fields.Many2one('account.move', string='Facture', check_company=True)
    partner_id = fields.Many2one('res.partner', string='Fournisseur')
    payment_move_id = fields.Many2one('account.move', string='Règlement', check_company=True)
    cash_limit_exceeded = fields.Boolean(
        string='Plafond espèces dépassé',
        compute='_compute_cash_limit_exceeded',
        help="Règlement en espèces au-delà des plafonds de l'art. 106 CGI : TVA non déductible.",
    )
    ord = fields.Integer(string='N° Ordre')
    num_facture = fields.Char(string='N° Facture', size=50)
    description = fields.Char(string='Description')
    montant_ht = fields.Float(string='Montant HT', digits=(16, 2))
    montant_tva = fields.Float(
        string='Montant TVA',
        digits=(16, 2),
        compute='_compute_montant_tva',
        store=True,
        readonly=False,
        help="Reprise de la TVA comptabilisée lors de la génération ; recalculée (HT x taux) "
             "si le HT ou le taux est modifié manuellement.",
    )
    montant_ttc = fields.Float(
        string='Montant TTC',
        digits=(16, 2),
        compute='_compute_montant_ttc',
        store=True,
    )
    if_fournisseur = fields.Char(
        string='IF Fournisseur', size=8,
        compute='_compute_supplier_data', store=True, readonly=False,
    )
    nom_fournisseur = fields.Char(
        string='Nom déclaré',
        compute='_compute_supplier_data', store=True, readonly=False,
        help="Nom ou raison sociale exporté dans la balise <nom>, repris de la fiche fournisseur.",
    )
    ice_fournisseur = fields.Char(
        string='ICE Fournisseur', size=15,
        compute='_compute_supplier_data', store=True, readonly=False,
    )
    missing_supplier_data = fields.Boolean(
        string='Données fournisseur incomplètes',
        compute='_compute_missing_supplier_data',
    )
    taux_tva = fields.Float(string='Taux TVA (%)', default=20.0)
    prorata = fields.Float(string='Prorata (%)', digits=(16, 2), default=100.0)
    mode_paiement = fields.Selection(
        MODE_PAIEMENT_SELECTION,
        string='Mode Paiement',
        default=DEFAULT_MODE_PAIEMENT,
    )
    date_paiement = fields.Date(string='Date Paiement')
    date_facture = fields.Date(string='Date Facture')

    @api.model
    def _get_partner_supplier_values(self, partner):
        partner = partner.commercial_partner_id
        return {
            'nom_fournisseur': partner.name or '',
            'if_fournisseur': partner._l10n_ma_get_if() if partner else '',
            'ice_fournisseur': partner._l10n_ma_get_ice() if partner else '',
        }

    @api.depends('partner_id')
    def _compute_supplier_data(self):
        for line in self:
            if line.partner_id:
                line.update(self._get_partner_supplier_values(line.partner_id))
            else:
                line.nom_fournisseur = line.nom_fournisseur
                line.if_fournisseur = line.if_fournisseur
                line.ice_fournisseur = line.ice_fournisseur

    @api.depends('partner_id.country_id', 'nom_fournisseur', 'if_fournisseur', 'ice_fournisseur')
    def _compute_missing_supplier_data(self):
        for line in self:
            line.missing_supplier_data = not line.nom_fournisseur or (
                line._is_identifiers_required()
                and not (line.if_fournisseur and line.ice_fournisseur)
            )

    def action_open_partner(self):
        """Fiche fournisseur simplifiée (IF, ICE, pays) en fenêtre, pour compléter les données."""
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_("Aucun fournisseur n'est rattaché à cette ligne."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Fiche fournisseur'),
            'res_model': 'res.partner',
            'res_id': self.partner_id.commercial_partner_id.id,
            'view_mode': 'form',
            'views': [(self.env.ref('l10n_ma_edi_simpl_tva.view_partner_form_l10n_ma_rd_quick').id, 'form')],
            'target': 'new',
            'context': {'l10n_ma_rd_declaration_id': self.declaration_id.id},
        }

    @api.depends(
        'declaration_id.line_ids.mode_paiement', 'declaration_id.line_ids.montant_ttc',
        'declaration_id.line_ids.date_paiement', 'declaration_id.line_ids.partner_id',
        'declaration_id.company_id.l10n_ma_rd_cash_daily_limit',
        'declaration_id.company_id.l10n_ma_rd_cash_monthly_limit',
    )
    def _compute_cash_limit_exceeded(self):
        flagged = self.browse()
        for declaration in self.declaration_id:
            flagged |= declaration._get_cash_limit_warnings()[0]
        for line in self:
            line.cash_limit_exceeded = line in flagged

    def _is_identifiers_required(self):
        """IF et ICE obligatoires sauf pour un fournisseur établi hors du Maroc."""
        self.ensure_one()
        country = self.partner_id.country_id
        return not country or country.code == 'MA'

    @api.depends('montant_ht', 'taux_tva')
    def _compute_montant_tva(self):
        for line in self:
            line.montant_tva = round((line.montant_ht or 0.0) * (line.taux_tva or 0.0) / 100.0, 2)

    @api.depends('montant_ht', 'montant_tva')
    def _compute_montant_ttc(self):
        for line in self:
            line.montant_ttc = round((line.montant_ht or 0.0) + (line.montant_tva or 0.0), 2)

    @api.constrains('prorata')
    def _check_prorata(self):
        for line in self:
            if not 0.0 < line.prorata <= 100.0:
                raise ValidationError(_("Le prorata doit être compris entre 0 (exclu) et 100."))

    @api.constrains('if_fournisseur')
    def _check_if_fournisseur(self):
        for line in self:
            if line.if_fournisseur and not line.if_fournisseur.isdigit():
                raise ValidationError(_("L'identifiant fiscal du fournisseur doit contenir uniquement des chiffres."))

    @api.constrains('ice_fournisseur')
    def _check_ice_fournisseur(self):
        for line in self:
            if line.ice_fournisseur and (
                not line.ice_fournisseur.isdigit() or len(line.ice_fournisseur) != 15
            ):
                raise ValidationError(_("L'ICE du fournisseur doit contenir exactement 15 chiffres."))
