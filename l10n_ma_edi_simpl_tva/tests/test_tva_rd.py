from datetime import date
from unittest.mock import patch

import psycopg2
from lxml import etree

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_ma_edi_simpl_tva.models import tva_rd_declaration


@tagged('post_install', '-at_install')
class TestTvaRdDeclaration(AccountTestInvoicingCommon):

    @classmethod
    def get_default_groups(cls):
        # Ne pas dépendre des groupes par défaut configurés dans la base
        return super().get_default_groups() | cls.env.ref('base.group_partner_manager')

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.write({
            'vat': '1668594',
            'l10n_ma_rd_regime_tva': '1',
            'l10n_ma_rd_periodicite': 'monthly',
            'l10n_ma_rd_prorata': 100.0,
        })
        # Copie de la taxe d'achat du plan : répartition vers un compte de TVA récupérable
        purchase_tax = cls.company_data['default_tax_purchase']
        cls.tax_10 = purchase_tax.copy({'name': 'TVA RD 10', 'amount': 10.0})
        cls.tax_20 = purchase_tax.copy({'name': 'TVA RD 20', 'amount': 20.0})
        cls.tax_10_non_deductible = purchase_tax.copy({'name': 'TVA RD 10 ND', 'amount': 10.0})
        (cls.tax_10_non_deductible.repartition_line_ids.filtered(
            lambda r: r.repartition_type == 'tax'
        )).account_id = False
        cls.partner_rd = cls.env['res.partner'].create({
            'name': 'Fournisseur RD',
            'vat': '3300624',
            'company_registry': '126587412555555',
            'company_id': False,
            'property_account_payable_id': cls.company_data['default_account_payable'].id,
        })
        cls.cash_account = cls.env['account.account'].create({
            'code': '999001',
            'name': 'Cash RD Test',
            'account_type': 'asset_cash',
        })
        cls.cash_journal = cls.env['account.journal'].create({
            'name': 'Cash RD Test',
            'type': 'cash',
            'code': 'CRDT',
            'company_id': cls.company.id,
            'default_account_id': cls.cash_account.id,
        })
        cls.cash_journal.outbound_payment_method_line_ids[0].write({
            'payment_account_id': cls.cash_account.id,
        })
        cls.eur = cls.setup_other_currency('EUR', rates=[('2026-01-01', 2.0)])
        cls.partner_bank = cls.env['res.partner'].create({
            'name': 'Banque RD',
            'vat': '1000001',
            'company_registry': '001234567000089',
            'company_id': False,
        })

    def _create_bill(self, price_unit=1000.10, tax=None, invoice_date=None, name='facture eau',
                     move_type='in_invoice', partner=None, currency=None):
        tax = tax or self.tax_10
        bill_date = invoice_date or date(2026, 5, 10)
        vals = {}
        if currency:
            vals['currency_id'] = currency.id
        return self.env['account.move'].create({
            **vals,
            'move_type': move_type,
            'partner_id': (partner or self.partner_rd).id,
            'invoice_date': bill_date,
            'date': bill_date,
            'invoice_line_ids': [(0, 0, {
                'name': name,
                'quantity': 1.0,
                'price_unit': price_unit,
                'tax_ids': [(6, 0, tax.ids)],
                'account_id': self.company_data['default_account_expense'].id,
            })],
        })

    def _pay(self, bill, pay_date, journal=None, method_line=None, amount=None):
        journal = journal or self.company_data['default_journal_bank']
        inbound = bill.move_type == 'in_refund'
        if inbound:
            method_line = method_line or journal.inbound_payment_method_line_ids[0]
        else:
            method_line = method_line or self.outbound_payment_method_line
        payment = self.env['account.payment'].create({
            'date': pay_date,
            'amount': amount or bill.amount_total,
            'currency_id': bill.currency_id.id,
            'payment_type': 'inbound' if inbound else 'outbound',
            'partner_type': 'supplier',
            'partner_id': bill.commercial_partner_id.id,
            'journal_id': journal.id,
            'payment_method_line_id': method_line.id,
            'company_id': bill.company_id.id,
        })
        payment.action_post()
        payable_line = payment.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type == 'liability_payable' and l.balance
        )[:1]
        bill.js_assign_outstanding_line(payable_line.id)
        return payment

    def _create_declaration(self, periode='5', periodicite='monthly', regime='1', annee='2026'):
        return self.env['tva.rd.declaration'].create({
            'name': f'RD TVA {periode}/{annee}',
            'company_id': self.company.id,
            'annee': annee,
            'periodicite': periodicite,
            'periode': periode,
            'regime_comptabilite': regime,
        })

    def test_regime_and_periode_mapping(self):
        decl_monthly = self._create_declaration(periode='10', periodicite='monthly')
        self.assertEqual(decl_monthly.regime, '1')
        self.assertEqual(decl_monthly._get_periode_xml(), '10')

        decl_quarterly = self._create_declaration(periode='T2', periodicite='quarterly')
        self.assertEqual(decl_quarterly.regime, '2')
        self.assertEqual(decl_quarterly._get_periode_xml(), '2')

    def test_amounts_match_dgi_example(self):
        self.company.l10n_ma_rd_prorata = 70.0
        bill = self._create_bill(price_unit=1000.10, tax=self.tax_10)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(len(decl.line_ids), 1)
        line = decl.line_ids
        self.assertEqual(line.montant_ht, 1000.10)
        self.assertEqual(line.taux_tva, 10.0)
        self.assertEqual(line.prorata, 70.0)
        self.assertEqual(line.montant_tva, 100.01)
        self.assertEqual(line.montant_ttc, 1100.11)

        root = etree.fromstring(decl._generate_xml_content())
        rd = root.find('./releveDeductions/rd')
        self.assertEqual(rd.findtext('mht'), '1000.10')
        self.assertEqual(rd.findtext('tva'), '100.01')
        self.assertEqual(rd.findtext('ttc'), '1100.11')
        self.assertEqual(rd.findtext('tx'), '10.00')
        self.assertEqual(rd.findtext('prorata'), '70')

    def test_xml_structure_matches_annexe(self):
        bill = self._create_bill(price_unit=1000.10, tax=self.tax_10)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        root = etree.fromstring(decl._generate_xml_content())

        self.assertEqual(root.tag, 'DeclarationReleveDeduction')
        header_tags = [child.tag for child in root if child.tag != 'releveDeductions']
        self.assertEqual(header_tags, ['identifiantFiscal', 'annee', 'periode', 'regime'])
        self.assertEqual(root.findtext('identifiantFiscal'), '1668594')
        self.assertEqual(root.findtext('annee'), '2026')
        self.assertEqual(root.findtext('periode'), '5')
        self.assertEqual(root.findtext('regime'), '1')

        rd = root.find('./releveDeductions/rd')
        self.assertEqual(
            [child.tag for child in rd],
            ['ord', 'num', 'des', 'mht', 'tva', 'ttc', 'refF', 'tx', 'prorata', 'mp', 'dpai', 'dfac'],
        )
        self.assertEqual([child.tag for child in rd.find('refF')], ['if', 'nom', 'ice'])
        self.assertEqual(rd.findtext('refF/if'), '3300624')
        self.assertEqual(rd.findtext('refF/nom'), 'Fournisseur RD')
        self.assertEqual(rd.findtext('refF/ice'), '126587412555555')
        self.assertEqual([child.tag for child in rd.find('mp')], ['id'])
        self.assertEqual(rd.findtext('dfac'), '2026-05-10')
        self.assertEqual(rd.findtext('dpai'), '2026-05-20')

    def test_mode_paiement_codes(self):
        bill_cash = self._create_bill(price_unit=100.0, tax=self.tax_10, name='facture cash')
        bill_cash.action_post()
        self._pay(bill_cash, date(2026, 5, 15), journal=self.cash_journal,
                  method_line=self.cash_journal.outbound_payment_method_line_ids[0])

        bill_bank = self._create_bill(price_unit=200.0, tax=self.tax_10, name='facture virement')
        bill_bank.action_post()
        self._pay(bill_bank, date(2026, 5, 16))

        bill_unpaid = self._create_bill(price_unit=300.0, tax=self.tax_10, name='facture impayee')
        bill_unpaid.action_post()

        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        modes = {
            line.move_id: (line.mode_paiement, line.date_paiement)
            for line in decl.line_ids
        }
        self.assertEqual(modes[bill_cash], ('1', date(2026, 5, 15)))
        self.assertEqual(modes[bill_bank], ('4', date(2026, 5, 16)))
        self.assertNotIn(bill_unpaid, modes)

    def test_unpaid_invoice_excluded_whatever_regime(self):
        bill = self._create_bill()
        bill.action_post()
        for regime, periode in (('1', '5'), ('2', '6')):
            decl = self._create_declaration(periode=periode, regime=regime)
            decl.action_generate_lines()
            self.assertFalse(decl.line_ids)

    def test_debit_regime_uses_payment_date(self):
        bill = self._create_bill(invoice_date=date(2026, 5, 10))
        bill.action_post()
        self._pay(bill, date(2026, 6, 5))

        decl_may = self._create_declaration(periode='5', regime='1')
        decl_may.action_generate_lines()
        self.assertFalse(decl_may.line_ids)

        decl_june = self._create_declaration(periode='6', regime='1')
        decl_june.action_generate_lines()
        self.assertEqual(decl_june.line_ids.move_id, bill)
        self.assertEqual(decl_june.line_ids.date_paiement, date(2026, 6, 5))

    def test_partial_payments_split_across_periods(self):
        bill = self._create_bill(price_unit=1000.0, invoice_date=date(2026, 5, 10))
        bill.action_post()
        # TTC 1100 : 440 payés en mai (40 %), 660 en juin (60 %)
        self._pay(bill, date(2026, 5, 20), amount=440.0)
        self._pay(bill, date(2026, 6, 10), amount=660.0)

        decl_may = self._create_declaration(periode='5')
        decl_may.action_generate_lines()
        self.assertEqual(len(decl_may.line_ids), 1)
        self.assertEqual(decl_may.line_ids.montant_ht, 400.0)
        self.assertEqual(decl_may.line_ids.montant_tva, 40.0)
        self.assertEqual(decl_may.line_ids.montant_ttc, 440.0)
        self.assertEqual(decl_may.line_ids.date_paiement, date(2026, 5, 20))

        decl_june = self._create_declaration(periode='6')
        decl_june.action_generate_lines()
        self.assertEqual(len(decl_june.line_ids), 1)
        self.assertEqual(decl_june.line_ids.montant_ht, 600.0)
        self.assertEqual(decl_june.line_ids.montant_ttc, 660.0)
        self.assertEqual(decl_june.line_ids.date_paiement, date(2026, 6, 10))

    def test_two_payments_same_period_give_two_lines(self):
        bill = self._create_bill(price_unit=1000.0)
        bill.action_post()
        cash_line = self.cash_journal.outbound_payment_method_line_ids[0]
        self._pay(bill, date(2026, 5, 15), journal=self.cash_journal, method_line=cash_line, amount=550.0)
        self._pay(bill, date(2026, 5, 25), amount=550.0)
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        lines = decl.line_ids.sorted('ord')
        self.assertEqual(lines.mapped('ord'), [1, 2])
        self.assertEqual(lines.mapped('montant_ht'), [500.0, 500.0])
        self.assertEqual(lines.mapped('mode_paiement'), ['1', '4'])
        self.assertEqual(lines.mapped('date_paiement'), [date(2026, 5, 15), date(2026, 5, 25)])

    def test_foreign_currency_amounts_in_company_currency(self):
        # 1 devise société = 2 EUR : 1000 EUR HT -> 500 en devise société
        bill = self._create_bill(price_unit=1000.0, currency=self.eur)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.montant_ht, 500.0)
        self.assertEqual(decl.line_ids.montant_tva, 50.0)

    def test_supplier_data_taken_from_commercial_partner(self):
        contact = self.env['res.partner'].create({
            'name': 'Contact Comptabilité',
            'type': 'contact',
            'parent_id': self.partner_rd.id,
        })
        bill = self._create_bill(partner=contact)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.nom_fournisseur, 'Fournisseur RD')
        self.assertEqual(decl.line_ids.if_fournisseur, '3300624')
        self.assertEqual(decl.line_ids.ice_fournisseur, '126587412555555')

    def test_refund_offset_against_invoice_is_not_a_payment(self):
        bill = self._create_bill(price_unit=1000.0)
        bill.action_post()
        refund = self._create_bill(price_unit=200.0, move_type='in_refund', name='avoir')
        refund.action_post()
        refund_line = refund.line_ids.filtered(lambda l: l.account_id.account_type == 'liability_payable')
        bill.js_assign_outstanding_line(refund_line.id)
        self._pay(bill, date(2026, 5, 20), amount=880.0)
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.move_id, bill)
        self.assertEqual(decl.line_ids.montant_ht, 800.0)

    def test_encaissement_regime_filters_by_payment_date(self):
        bill = self._create_bill(invoice_date=date(2026, 5, 10))
        bill.action_post()
        self._pay(bill, date(2026, 6, 5))

        decl_may = self._create_declaration(periode='5', regime='2')
        decl_may.action_generate_lines()
        self.assertEqual(len(decl_may.line_ids), 0)

        decl_june = self._create_declaration(periode='6', regime='2')
        decl_june.action_generate_lines()
        self.assertEqual(len(decl_june.line_ids), 1)
        self.assertEqual(decl_june.line_ids.date_paiement, date(2026, 6, 5))
        root = etree.fromstring(decl_june._generate_xml_content())
        self.assertEqual(root.findtext('./releveDeductions/rd/dpai'), '2026-06-05')

    def test_refund_included_with_negative_amounts(self):
        refund = self._create_bill(
            price_unit=1000.10, move_type='in_refund', invoice_date=date(2026, 5, 12),
            name='avoir fournisseur',
        )
        refund.action_post()
        self._pay(refund, date(2026, 5, 18))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(len(decl.line_ids), 1)
        line = decl.line_ids
        self.assertEqual(line.montant_ht, -1000.10)
        self.assertEqual(line.montant_tva, -100.01)
        self.assertEqual(line.montant_ttc, -1100.11)
        root = etree.fromstring(decl._generate_xml_content())
        self.assertEqual(root.findtext('./releveDeductions/rd/mht'), '-1000.10')

    def test_multi_rate_invoice_generates_one_line_per_rate(self):
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_rd.id,
            'invoice_date': date(2026, 5, 10),
            'date': date(2026, 5, 10),
            'invoice_line_ids': [
                (0, 0, {
                    'name': 'ligne 10 pourcent',
                    'quantity': 1.0,
                    'price_unit': 1000.0,
                    'tax_ids': [(6, 0, self.tax_10.ids)],
                    'account_id': self.company_data['default_account_expense'].id,
                }),
                (0, 0, {
                    'name': 'ligne 20 pourcent',
                    'quantity': 1.0,
                    'price_unit': 500.0,
                    'tax_ids': [(6, 0, self.tax_20.ids)],
                    'account_id': self.company_data['default_account_expense'].id,
                }),
            ],
        })
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        amounts = {line.taux_tva: line.montant_ht for line in decl.line_ids}
        self.assertEqual(amounts, {10.0: 1000.0, 20.0: 500.0})
        self.assertEqual(sorted(decl.line_ids.mapped('ord')), [1, 2])

    def test_ice_fallback_company_registry(self):
        partner = self.env['res.partner'].create({
            'name': 'Fournisseur ICE Registry',
            'company_id': False,
            'company_registry': '002353313000011',
            'vat': '3300625',
            'property_account_payable_id': self.company_data['default_account_payable'].id,
        })
        bill = self._create_bill(partner=partner)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.ice_fournisseur, '002353313000011')

    def test_unique_declaration_per_period(self):
        self._create_declaration(periode='5')
        with self.assertRaises(psycopg2.IntegrityError):
            self._create_declaration(periode='5')

    def test_validate_rejects_incomplete_lines(self):
        bill = self._create_bill()
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        decl.line_ids.num_facture = False
        with self.assertRaises(UserError):
            decl.action_validate()

    def test_line_constraints_ice(self):
        bill = self._create_bill()
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertTrue(decl.line_ids)
        with self.assertRaises(ValidationError):
            decl.line_ids.ice_fournisseur = '123'

    def test_export_blocked_on_non_conformity(self):
        bill = self._create_bill()
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        decl.line_ids.num_facture = False
        with self.assertRaises(UserError):
            decl._generate_xml_content()

    def test_wizard_quarterly_declaration_name(self):
        wizard = self.env['tva.rd.wizard'].create({
            'annee': '2026',
            'periodicite': 'quarterly',
            'periode_trimestrielle': 'T1',
            'company_id': self.company.id,
        })
        action = wizard.action_create_declaration()
        decl = self.env['tva.rd.declaration'].browse(action['res_id'])
        self.assertEqual(decl.name, 'RD TVA T1/2026')
        self.assertEqual(decl.regime, '2')

    def test_vat_taken_from_tax_lines(self):
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_rd.id,
            'invoice_date': date(2026, 5, 10),
            'date': date(2026, 5, 10),
            'invoice_line_ids': [(0, 0, {
                'name': f'petite ligne {i}',
                'quantity': 1.0,
                'price_unit': 0.05,
                'tax_ids': [(6, 0, self.tax_10.ids)],
                'account_id': self.company_data['default_account_expense'].id,
            }) for i in range(3)],
        })
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        line = decl.line_ids
        self.assertEqual(line.montant_ht, bill.amount_untaxed)
        self.assertEqual(line.montant_tva, bill.amount_tax)
        self.assertEqual(line.montant_ttc, bill.amount_total)

    def test_zero_rate_lines_excluded(self):
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_rd.id,
            'invoice_date': date(2026, 5, 10),
            'date': date(2026, 5, 10),
            'invoice_line_ids': [
                (0, 0, {
                    'name': 'ligne taxée',
                    'quantity': 1.0,
                    'price_unit': 1000.0,
                    'tax_ids': [(6, 0, self.tax_10.ids)],
                    'account_id': self.company_data['default_account_expense'].id,
                }),
                (0, 0, {
                    'name': 'ligne exonérée',
                    'quantity': 1.0,
                    'price_unit': 300.0,
                    'tax_ids': [(5, 0, 0)],
                    'account_id': self.company_data['default_account_expense'].id,
                }),
            ],
        })
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(len(decl.line_ids), 1)
        self.assertEqual(decl.line_ids.taux_tva, 10.0)
        # Seule la base taxée est reprise : la ligne exonérée (300) est ignorée
        self.assertEqual(decl.line_ids.montant_ht, 1000.0)
        self.assertEqual(decl.line_ids.montant_tva, 100.0)

    def test_non_deductible_vat_excluded(self):
        bill = self._create_bill(price_unit=1000.0, tax=self.tax_10_non_deductible)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertFalse(decl.line_ids)

    def _create_bank_fees(self, fee_date, amount=100.0, partner=None):
        bank_journal = self.company_data['default_journal_bank']
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': bank_journal.id,
            'date': fee_date,
            'ref': 'FRAIS-BQ-05',
            'line_ids': [
                (0, 0, {
                    'name': 'Frais de tenue de compte',
                    'account_id': self.company_data['default_account_expense'].id,
                    'debit': amount,
                    'tax_ids': [(6, 0, self.tax_10.ids)],
                    'partner_id': (partner or self.partner_bank).id,
                }),
                (0, 0, {
                    'name': 'Frais de tenue de compte',
                    'account_id': bank_journal.default_account_id.id,
                    'credit': round(amount * 1.1, 2),
                    'partner_id': (partner or self.partner_bank).id,
                }),
            ],
        })
        move.action_post()
        return move

    def test_bank_fees_entry_included(self):
        fees = self._create_bank_fees(date(2026, 5, 31))
        self.assertTrue(fees.line_ids.filtered('tax_line_id'), "La ligne de TVA doit être générée")
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(len(decl.line_ids), 1)
        line = decl.line_ids
        self.assertEqual(line.move_id, fees)
        self.assertEqual(line.payment_move_id, fees)
        self.assertEqual((line.montant_ht, line.montant_tva, line.montant_ttc), (100.0, 10.0, 110.0))
        self.assertEqual(line.taux_tva, 10.0)
        self.assertEqual(line.mode_paiement, '3')
        self.assertEqual(line.date_paiement, date(2026, 5, 31))
        self.assertEqual(line.date_facture, date(2026, 5, 31))
        self.assertEqual(line.num_facture, 'FRAIS-BQ-05')
        self.assertEqual(line.nom_fournisseur, 'Banque RD')
        self.assertEqual(line.if_fournisseur, '1000001')
        self.assertEqual(line.ice_fournisseur, '001234567000089')
        root = etree.fromstring(decl._generate_xml_content())
        self.assertEqual(root.findtext('./releveDeductions/rd/tx'), '10.00')

        decl_june = self._create_declaration(periode='6')
        decl_june.action_generate_lines()
        self.assertFalse(decl_june.line_ids)

    def test_bank_fees_and_invoices_numbered_together(self):
        bill = self._create_bill(price_unit=1000.0)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        self._create_bank_fees(date(2026, 5, 10))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        lines = decl.line_ids.sorted('ord')
        self.assertEqual(lines.mapped('ord'), [1, 2])
        self.assertEqual(lines.mapped('date_paiement'), [date(2026, 5, 10), date(2026, 5, 20)])

    def test_cash_basis_vat_not_counted_twice(self):
        # Les taxes d'achat de l10n_ma sont exigibles au paiement : Odoo génère une
        # écriture de TVA d'encaissement (CABA) au règlement, qui ne doit pas être reprise.
        self.company.tax_exigibility = True
        transition = self.env['account.account'].create({
            'code': '345590',
            'name': 'TVA en attente RD',
            'account_type': 'asset_current',
            'reconcile': True,
        })
        tax_caba = self.tax_10.copy({
            'name': 'TVA RD 10 encaissement',
            'tax_exigibility': 'on_payment',
            'cash_basis_transition_account_id': transition.id,
        })
        bill = self._create_bill(price_unit=1000.0, tax=tax_caba)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20), amount=550.0)
        caba = self.env['account.move'].search([('tax_cash_basis_origin_move_id', '=', bill.id)])
        self.assertTrue(caba, "Une écriture CABA doit exister")
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(len(decl.line_ids), 1)
        self.assertEqual(decl.line_ids.move_id, bill)
        self.assertEqual(decl.line_ids.montant_ht, 500.0)
        self.assertEqual(decl.line_ids.montant_tva, 50.0)

    def _create_validated_declaration(self):
        bill = self._create_bill(price_unit=1000.0)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        decl.action_validate()
        return decl

    def _assert_validation_error(self, decl, fragment):
        with self.assertRaises(UserError) as ctx:
            decl.action_validate()
        self.assertIn(fragment, str(ctx.exception))
        self.assertEqual(decl.state, 'draft')

    def test_supplier_identifiers_required_for_moroccan_supplier(self):
        partner = self.env['res.partner'].create({
            'name': 'Fournisseur sans IF',
            'company_id': False,
        })
        bill = self._create_bill(partner=partner)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.partner_id, partner)
        self._assert_validation_error(decl, "identifiant fiscal (IF) du fournisseur manquant")
        self._assert_validation_error(decl, "ICE du fournisseur manquant")

    def test_supplier_identifiers_optional_for_foreign_supplier(self):
        partner = self.env['res.partner'].create({
            'name': 'Foreign Supplier',
            'company_id': False,
            'country_id': self.env.ref('base.fr').id,
            'vat': 'FR94942617283',
        })
        bill = self._create_bill(partner=partner)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        decl.action_validate()
        self.assertEqual(decl.state, 'validated')
        rd = etree.fromstring(decl._generate_xml_content()).find('./releveDeductions/rd')
        self.assertEqual(rd.findtext('refF/if'), '')
        self.assertEqual(rd.findtext('refF/ice'), '')

    def test_illegal_vat_rate_blocked(self):
        tax_15 = self.tax_10.copy({'name': 'TVA RD 17', 'amount': 17.0})
        bill = self._create_bill(tax=tax_15)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.taux_tva, 17.0)
        self._assert_validation_error(decl, "taux de TVA 17 % non autorisé")

    def test_payment_date_required_and_within_period(self):
        bill = self._create_bill()
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        decl.line_ids.date_paiement = date(2026, 6, 2)
        self._assert_validation_error(decl, "hors de la période déclarée")
        decl.line_ids.date_paiement = False
        self._assert_validation_error(decl, "date de paiement manquante")

    def test_duplicate_ord_blocked(self):
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_rd.id,
            'invoice_date': date(2026, 5, 10),
            'date': date(2026, 5, 10),
            'invoice_line_ids': [
                (0, 0, {
                    'name': f'ligne {tax.name}',
                    'quantity': 1.0,
                    'price_unit': 100.0,
                    'tax_ids': [(6, 0, tax.ids)],
                    'account_id': self.company_data['default_account_expense'].id,
                }) for tax in (self.tax_10, self.tax_20)
            ],
        })
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        decl.line_ids.write({'ord': 1})
        self._assert_validation_error(decl, "Numéros d'ordre en double : 1")

    def test_prorata_bounds(self):
        decl = self._create_validated_declaration()
        decl.action_reset_draft()
        with self.assertRaises(ValidationError):
            decl.line_ids.prorata = 120.0
        with self.assertRaises(ValidationError):
            self.company.l10n_ma_rd_prorata = 0.0

    def test_validated_declaration_is_locked(self):
        decl = self._create_validated_declaration()
        with self.assertRaises(UserError):
            decl.line_ids.num_facture = 'MODIF'
        with self.assertRaises(UserError):
            decl.line_ids.unlink()
        with self.assertRaises(UserError):
            decl.periode = '6'
        with self.assertRaises(UserError):
            decl.action_generate_lines()
        with self.assertRaises(UserError):
            decl.action_validate()
        with self.assertRaises(UserError):
            decl.unlink()

        decl.action_reset_draft()
        decl.line_ids.num_facture = 'MODIF'
        self.assertEqual(decl.line_ids.num_facture, 'MODIF')

    def test_exported_declaration_is_locked(self):
        decl = self._create_validated_declaration()
        decl.action_export_zip()
        self.assertEqual(decl.state, 'exported')
        with self.assertRaises(UserError):
            decl.line_ids.montant_ht = 1.0
        # Réexport autorisé depuis l'état exporté
        decl.action_export_xml()

    def test_export_requires_validation(self):
        bill = self._create_bill()
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        with self.assertRaises(UserError):
            decl.action_export_zip()
        with self.assertRaises(UserError):
            decl.action_reset_draft()

    def test_export_reruns_controls(self):
        decl = self._create_validated_declaration()
        self.company.vat = False
        with self.assertRaises(UserError) as ctx:
            decl.action_export_zip()
        self.assertIn("identifiant fiscal de la société est obligatoire", str(ctx.exception))
        self.assertEqual(decl.state, 'validated')

    def test_wizard_rejects_existing_period(self):
        self._create_declaration(periode='5')
        wizard = self.env['tva.rd.wizard'].create({
            'annee': '2026',
            'periodicite': 'monthly',
            'periode_mensuelle': '5',
            'company_id': self.company.id,
        })
        with self.assertRaises(UserError) as ctx:
            wizard.action_create_declaration()
        self.assertIn("existe déjà", str(ctx.exception))

    def _pay_cash(self, bill, pay_date, amount=None):
        return self._pay(
            bill, pay_date, journal=self.cash_journal,
            method_line=self.cash_journal.outbound_payment_method_line_ids[0], amount=amount,
        )

    def test_mode_configured_on_payment_method_line(self):
        self.outbound_payment_method_line.l10n_ma_rd_mode_paiement = '5'
        bill = self._create_bill()
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.mode_paiement, '5')

    def test_mode_effet_detected_from_method_name(self):
        self.outbound_payment_method_line.name = 'LCN fournisseurs'
        bill = self._create_bill()
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.mode_paiement, '5')

    def test_mode_compensation_with_customer_receivable(self):
        bill = self._create_bill(price_unit=1000.0)
        bill.action_post()
        netting = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.company_data['default_journal_misc'].id,
            'date': date(2026, 5, 25),
            'ref': 'Compensation client / fournisseur',
            'line_ids': [
                (0, 0, {
                    'account_id': self.company_data['default_account_payable'].id,
                    'partner_id': self.partner_rd.id,
                    'debit': 1100.0,
                }),
                (0, 0, {
                    'account_id': self.company_data['default_account_receivable'].id,
                    'partner_id': self.partner_rd.id,
                    'credit': 1100.0,
                }),
            ],
        })
        netting.action_post()
        (netting.line_ids + bill.line_ids).filtered(
            lambda l: l.account_id.account_type == 'liability_payable'
        ).reconcile()
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.mode_paiement, '6')
        self.assertEqual(decl.line_ids.payment_move_id, netting)
        self.assertEqual(decl.line_ids.date_paiement, date(2026, 5, 25))

    def test_journal_default_mode_for_direct_entries(self):
        self.company_data['default_journal_bank'].l10n_ma_rd_mode_paiement = '4'
        self._create_bank_fees(date(2026, 5, 31))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.mode_paiement, '4')

    def test_cash_daily_limit_flagged_not_blocking(self):
        big = self._create_bill(price_unit=5000.0)  # 5500 TTC
        big.action_post()
        self._pay_cash(big, date(2026, 5, 15))
        small = self._create_bill(price_unit=100.0)
        small.action_post()
        self._pay_cash(small, date(2026, 5, 16))
        by_bank = self._create_bill(price_unit=9000.0)
        by_bank.action_post()
        self._pay(by_bank, date(2026, 5, 15))

        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        flagged = decl.line_ids.filtered('cash_limit_exceeded')
        self.assertEqual(flagged.move_id, big)
        self.assertIn('art. 106', decl.warning_message)
        self.assertIn('5500.00', decl.warning_message)
        decl.action_validate()
        self.assertEqual(decl.state, 'validated')

    def test_cash_monthly_limit_cumulates_per_supplier(self):
        self.company.l10n_ma_rd_cash_monthly_limit = 6000.0
        bills = self.env['account.move']
        for day in (5, 12):
            bill = self._create_bill(price_unit=4000.0 / 1.1)
            bill.action_post()
            self._pay_cash(bill, date(2026, 5, day))
            bills |= bill
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.filtered('cash_limit_exceeded').move_id, bills)
        self.assertIn('par mois', decl.warning_message)
        self.assertNotIn('par jour', decl.warning_message)

    def test_cash_limits_disabled(self):
        self.company.write({'l10n_ma_rd_cash_daily_limit': 0.0, 'l10n_ma_rd_cash_monthly_limit': 0.0})
        bill = self._create_bill(price_unit=50000.0)
        bill.action_post()
        self._pay_cash(bill, date(2026, 5, 15))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertFalse(decl.line_ids.cash_limit_exceeded)
        self.assertFalse(decl.warning_message)

    def test_zip_size_limit(self):
        decl = self._create_validated_declaration()
        with patch.object(tva_rd_declaration, 'MAX_EDI_ZIP_SIZE', 10):
            with self.assertRaises(UserError) as ctx:
                decl.action_export_zip()
        self.assertIn('SIMPL-TVA refuse', str(ctx.exception))
        self.assertEqual(decl.state, 'validated')

    def _declaration_with_incomplete_supplier(self):
        partner = self.env['res.partner'].create({'name': 'Fournisseur à compléter', 'company_id': False})
        bill = self._create_bill(partner=partner)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        return decl, partner

    def test_missing_supplier_data_flag(self):
        decl, partner = self._declaration_with_incomplete_supplier()
        self.assertTrue(decl.line_ids.missing_supplier_data)
        partner.country_id = self.env.ref('base.fr')
        decl.line_ids.invalidate_recordset(['missing_supplier_data'])
        self.assertFalse(decl.line_ids.missing_supplier_data)

    def test_open_partner_quick_form(self):
        contact = self.env['res.partner'].create({
            'name': 'Contact', 'type': 'contact', 'parent_id': self.partner_rd.id,
        })
        bill = self._create_bill(partner=contact)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        action = decl.line_ids.action_open_partner()
        self.assertEqual(action['res_model'], 'res.partner')
        self.assertEqual(action['res_id'], self.partner_rd.id)
        self.assertEqual(action['target'], 'new')
        self.assertEqual(action['context']['l10n_ma_rd_declaration_id'], decl.id)

    def test_quick_form_save_refreshes_declaration(self):
        decl, partner = self._declaration_with_incomplete_supplier()
        action = decl.line_ids.action_open_partner()
        partner.with_context(action['context']).write({
            'vat': '4455667',
            'company_registry': '002233445000066',
        })
        result = partner.with_context(action['context']).action_l10n_ma_rd_save_and_refresh()
        self.assertEqual(result['type'], 'ir.actions.act_window_close')
        self.assertEqual(decl.line_ids.if_fournisseur, '4455667')
        self.assertEqual(decl.line_ids.ice_fournisseur, '002233445000066')
        decl.action_validate()
        self.assertEqual(decl.state, 'validated')

    def test_validate_refreshes_supplier_data(self):
        decl, partner = self._declaration_with_incomplete_supplier()
        partner.write({'vat': '4455667', 'company_registry': '002233445000066'})
        decl.action_validate()
        self.assertEqual(decl.state, 'validated')
        self.assertEqual(decl.line_ids.ice_fournisseur, '002233445000066')

    def test_refresh_keeps_manual_values_and_respects_lock(self):
        decl, partner = self._declaration_with_incomplete_supplier()
        decl.line_ids.write({'if_fournisseur': '7778889', 'ice_fournisseur': '009988776000055'})
        decl.action_refresh_suppliers()
        self.assertEqual(decl.line_ids.if_fournisseur, '7778889')
        decl.action_validate()
        partner.vat = '1112223'
        partner.with_context(l10n_ma_rd_declaration_id=decl.id).action_l10n_ma_rd_save_and_refresh()
        self.assertEqual(decl.line_ids.if_fournisseur, '7778889')
        with self.assertRaises(UserError):
            decl.action_refresh_suppliers()

    def test_changing_line_partner_updates_supplier_data(self):
        decl, _partner = self._declaration_with_incomplete_supplier()
        decl.line_ids.partner_id = self.partner_rd
        self.assertEqual(decl.line_ids.nom_fournisseur, 'Fournisseur RD')
        self.assertEqual(decl.line_ids.if_fournisseur, '3300624')
        self.assertEqual(decl.line_ids.ice_fournisseur, '126587412555555')

    def _create_custom_declaration(self, date_from, date_to, periode='6', declared='monthly', annee='2026'):
        return self.env['tva.rd.declaration'].create({
            'name': f'RD TVA perso {periode}/{annee}',
            'company_id': self.company.id,
            'annee': annee,
            'periodicite': 'custom',
            'periodicite_declaree': declared,
            'periode': periode,
            'date_from': date_from,
            'date_to': date_to,
        })

    def _paid_bill(self, invoice_date, pay_date, price_unit=1000.0):
        bill = self._create_bill(price_unit=price_unit, invoice_date=invoice_date)
        bill.action_post()
        self._pay(bill, pay_date)
        return bill

    def test_custom_period_includes_forgotten_payments(self):
        forgotten = self._paid_bill(date(2026, 3, 5), date(2026, 3, 20))
        current = self._paid_bill(date(2026, 6, 2), date(2026, 6, 10))
        decl = self._create_custom_declaration(date(2026, 3, 1), date(2026, 6, 30))
        self.assertEqual(decl.periodicite_declaree, 'monthly')
        self.assertEqual(decl.regime, '1')
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.move_id, forgotten | current)
        decl.action_validate()
        root = etree.fromstring(decl._generate_xml_content())
        self.assertEqual(root.findtext('periode'), '6')
        self.assertEqual(root.findtext('regime'), '1')
        self.assertEqual(
            [rd.findtext('dpai') for rd in root.findall('./releveDeductions/rd')],
            ['2026-03-20', '2026-06-10'],
        )

    def test_custom_period_quarterly_declared(self):
        self._paid_bill(date(2026, 2, 5), date(2026, 2, 20))
        decl = self._create_custom_declaration(date(2026, 1, 1), date(2026, 6, 30), periode='T2', declared='quarterly')
        decl.action_generate_lines()
        self.assertEqual(decl.regime, '2')
        self.assertEqual(decl._get_periode_xml(), '2')
        self.assertEqual(len(decl.line_ids), 1)

    def test_payments_already_declared_are_excluded(self):
        march = self._paid_bill(date(2026, 3, 5), date(2026, 3, 20))
        april = self._paid_bill(date(2026, 4, 5), date(2026, 4, 20))
        decl_march = self._create_declaration(periode='3')
        decl_march.action_generate_lines()
        self.assertEqual(decl_march.line_ids.move_id, march)

        decl = self._create_custom_declaration(date(2026, 3, 1), date(2026, 6, 30))
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.move_id, april)

    def test_double_deduction_blocked_at_validation(self):
        self._paid_bill(date(2026, 3, 5), date(2026, 3, 20))
        decl = self._create_custom_declaration(date(2026, 3, 1), date(2026, 6, 30))
        decl.action_generate_lines()
        decl_march = self._create_declaration(periode='3')
        decl_march.action_generate_lines()
        self.assertFalse(decl_march.line_ids, "Le paiement est déjà dans la déclaration personnalisée")
        line = decl.line_ids
        decl_march.line_ids = [(0, 0, {
            'ord': 1,
            'move_id': line.move_id.id,
            'payment_move_id': line.payment_move_id.id,
            'partner_id': line.partner_id.id,
            'num_facture': line.num_facture,
            'description': line.description,
            'montant_ht': line.montant_ht,
            'taux_tva': line.taux_tva,
            'mode_paiement': line.mode_paiement,
            'date_paiement': line.date_paiement,
            'date_facture': line.date_facture,
        })]
        self._assert_validation_error(decl_march, "déjà déclaré dans RD TVA perso 6/2026")

    def test_one_year_deduction_delay(self):
        too_old = self._paid_bill(date(2025, 4, 2), date(2025, 4, 15))
        still_ok = self._paid_bill(date(2025, 6, 2), date(2025, 6, 10))
        decl = self._create_custom_declaration(date(2025, 4, 1), date(2026, 6, 30))
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.move_id, too_old | still_ok)
        self._assert_validation_error(decl, "délai de déduction d'un an dépassé")
        decl.line_ids.filtered(lambda l: l.move_id == too_old).unlink()
        decl.action_validate()
        self.assertEqual(decl.state, 'validated')

    def test_custom_dates_constraints(self):
        with self.assertRaises(ValidationError):
            self._create_custom_declaration(date(2026, 3, 1), date(2026, 7, 1))
        with self.assertRaises(ValidationError):
            self._create_custom_declaration(date(2026, 6, 30), date(2026, 3, 1))
        with self.assertRaises(ValidationError):
            self._create_custom_declaration(False, False)

    def test_wizard_custom_period(self):
        self._paid_bill(date(2026, 4, 5), date(2026, 4, 20))
        wizard = self.env['tva.rd.wizard'].create({
            'annee': '2026',
            'periodicite': 'custom',
            'periodicite_declaree': 'monthly',
            'periode_mensuelle': '6',
            'date_from': date(2026, 4, 1),
            'date_to': date(2026, 6, 30),
            'company_id': self.company.id,
        })
        decl = self.env['tva.rd.declaration'].browse(wizard.action_create_declaration()['res_id'])
        self.assertEqual(decl.name, 'RD TVA 06/2026 (paiements du 01/04/2026 au 30/06/2026)')
        self.assertEqual((decl.periodicite, decl.periodicite_declaree, decl.periode), ('custom', 'monthly', '6'))
        self.assertEqual(len(decl.line_ids), 1)

    def test_transitional_vat_rates_accepted(self):
        # Facture 2025 à 18 % (réforme LF 2024) payée en 2026 : déductible à son taux d'origine
        tax_18 = self.tax_10.copy({'name': 'TVA RD 18', 'amount': 18.0})
        bill = self._create_bill(tax=tax_18, invoice_date=date(2025, 12, 10))
        bill.action_post()
        self._pay(bill, date(2026, 1, 15))
        decl = self._create_declaration(periode='1')
        decl.action_generate_lines()
        decl.action_validate()
        self.assertEqual(decl.state, 'validated')

    def test_supplier_if_normalized_from_vat(self):
        partner = self.env['res.partner'].create({
            'name': 'Fournisseur MA préfixé',
            'company_id': False,
            'country_id': self.env.ref('base.ma').id,
            'vat': 'MA 1234-567',
            'company_registry': '001234567000089',
        })
        self.assertEqual(partner._l10n_ma_get_if(), '1234567')
        self.assertEqual(partner._l10n_ma_get_ice(), '001234567000089')
        bill = self._create_bill(partner=partner)
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        self.assertEqual(decl.line_ids.if_fournisseur, '1234567')
        self.assertEqual(decl.line_ids.ice_fournisseur, '001234567000089')
        decl.action_validate()
        self.assertEqual(decl.state, 'validated')

    def test_company_if_taken_from_vat(self):
        self.company.vat = 'MA 1668594'
        self.assertEqual(self.company._l10n_ma_get_if(), '1668594')
        bill = self._create_bill()
        bill.action_post()
        self._pay(bill, date(2026, 5, 20))
        decl = self._create_declaration(periode='5')
        decl.action_generate_lines()
        root = etree.fromstring(decl._generate_xml_content())
        self.assertEqual(root.findtext('identifiantFiscal'), '1668594')

    def test_moroccan_vat_label_is_if(self):
        self.assertEqual(self.env.ref('base.ma').vat_label, 'IF')
