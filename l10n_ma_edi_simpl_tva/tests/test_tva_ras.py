from datetime import date

from lxml import etree

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_ma_edi_simpl_tva.models.tva_ras_declaration import _get_ras_schema


@tagged('post_install', '-at_install')
class TestTvaRasDeclaration(AccountTestInvoicingCommon):

    @classmethod
    def get_default_groups(cls):
        # Ne pas dépendre des groupes par défaut configurés dans la base
        return super().get_default_groups() | cls.env.ref('base.group_partner_manager')

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.write({
            'vat': '1030709',
            'l10n_ma_ras_default_nature': '3',
        })
        purchase_tax = cls.company_data['default_tax_purchase']
        cls.tax_20 = purchase_tax.copy({'name': 'TVA RAS 20', 'amount': 20.0})
        cls.tax_10 = purchase_tax.copy({'name': 'TVA RAS 10', 'amount': 10.0})
        cls.tax_14 = purchase_tax.copy({'name': 'TVA RAS 14', 'amount': 14.0})
        cls.tax_18 = purchase_tax.copy({'name': 'TVA RAS 18', 'amount': 18.0})
        cls.supplier = cls.env['res.partner'].create({
            'name': 'Prestataire RAS',
            'is_company': True,
            'company_id': False,
            'vat': '1000003',
            'l10n_ma_ras_applicable': True,
        })
        cls.service = cls.env['product.product'].create({
            'name': 'Prestation de maintenance',
            'type': 'service',
            'purchase_ok': True,
        })
        cls.goods = cls.env['product.product'].create({
            'name': 'Marchandise',
            'type': 'consu',
            'purchase_ok': True,
        })
        cls.works = cls.env['product.product'].create({
            'name': 'Travaux de construction',
            'type': 'service',
            'purchase_ok': True,
            'l10n_ma_ras_nature': '2',
        })
        cls.ras_account = cls.env['account.account'].create({
            'code': '445800',
            'name': 'État - TVA retenue à la source',
            'account_type': 'liability_current',
        })

    def _create_bill(self, lines=None, partner=None, invoice_date=date(2026, 6, 2), post=True):
        lines = lines or [(self.service, 1000.0, self.tax_20)]
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': (partner or self.supplier).id,
            'invoice_date': invoice_date,
            'date': invoice_date,
            'ref': 'FAC-RAS-001',
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id if product else False,
                'name': product.name if product else 'Honoraires',
                'quantity': 1.0,
                'price_unit': price,
                'tax_ids': [(6, 0, tax.ids)] if tax else [(5, 0, 0)],
                'account_id': self.company_data['default_account_expense'].id,
            }) for product, price, tax in lines],
        })
        if post:
            bill.action_post()
        return bill

    def _pay(self, bill, pay_date, amount):
        payment = self.env['account.payment'].create({
            'date': pay_date,
            'amount': amount,
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'partner_id': bill.commercial_partner_id.id,
            'journal_id': self.company_data['default_journal_bank'].id,
            'payment_method_line_id': self.outbound_payment_method_line.id,
        })
        payment.action_post()
        payable_line = payment.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type == 'liability_payable'
        )
        bill.js_assign_outstanding_line(payable_line.id)
        return payment

    def _pay_net(self, bill, pay_date=date(2026, 6, 20)):
        return self._pay(bill, pay_date, bill.amount_total - bill.l10n_ma_ras_amount)

    def _book_withholding(self, bill, entry_date=date(2026, 6, 20)):
        entry = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.company_data['default_journal_misc'].id,
            'date': entry_date,
            'ref': 'Retenue TVA',
            'line_ids': [
                (0, 0, {
                    'account_id': self.company_data['default_account_payable'].id,
                    'partner_id': bill.commercial_partner_id.id,
                    'debit': bill.l10n_ma_ras_amount,
                }),
                (0, 0, {
                    'account_id': self.ras_account.id,
                    'credit': bill.l10n_ma_ras_amount,
                }),
            ],
        })
        entry.action_post()
        (entry.line_ids + bill.line_ids).filtered(
            lambda l: l.account_id.account_type == 'liability_payable'
        ).reconcile()
        return entry

    def _create_declaration(self, periode='6', annee='2026', **vals):
        return self.env['tva.ras.declaration'].create({
            'name': f'RAS TVA {periode}/{annee}',
            'company_id': self.company.id,
            'annee': annee,
            'periodicite': 'monthly',
            'periode': periode,
            **vals,
        })

    def _generated(self, periode='6', **vals):
        decl = self._create_declaration(periode=periode, **vals)
        decl.action_generate_lines()
        return decl

    def test_bill_ras_fields(self):
        bill = self._create_bill()
        self.assertTrue(bill.l10n_ma_ras_applicable)
        self.assertEqual(bill.l10n_ma_ras_taux, '75')
        self.assertEqual(bill.invoice_line_ids.l10n_ma_ras_nature, '3')
        # 1000 HT x 20 % x 75 %
        self.assertEqual(bill.l10n_ma_ras_amount, 150.0)

    def test_full_net_payment_and_xml_matches_xsd(self):
        bill = self._create_bill()
        self._pay_net(bill)
        decl = self._generated()
        line = decl.line_ids
        self.assertEqual(len(line), 1)
        self.assertEqual(line.partner_id, self.supplier)
        self.assertEqual(line.ifu_fournisseur, '1000003')
        self.assertEqual((line.montant_ht, line.taux_tva, line.taux_retenue), (1000.0, 20.0, '75'))
        self.assertEqual((line.montant_tva, line.montant_retenu), (200.0, 150.0))
        self.assertEqual(decl.montant_retenu_total, 150.0)
        self.assertEqual(line.date_paiement, date(2026, 6, 20))
        self.assertEqual(line.date_operation, date(2026, 6, 2))

        decl.action_validate()
        root = etree.fromstring(decl._generate_xml_content())
        self.assertTrue(_get_ras_schema().validate(root))
        self.assertEqual(root.tag, 'VersementRetenueSources')
        self.assertEqual(
            [child.tag for child in root],
            ['identifiantFiscal', 'annee', 'periode', 'regime', 'fournisseurs'],
        )
        self.assertEqual(
            (root.findtext('identifiantFiscal'), root.findtext('annee'), root.findtext('periode'), root.findtext('regime')),
            ('1030709', '2026', '6', '1'),
        )
        node = root.find('./fournisseurs/fournisseur')
        self.assertEqual(
            [child.tag for child in node],
            ['ifuFournisseur', 'numFacture', 'datePaiement', 'dateOperation', 'refNatOpt',
             'montantHT', 'tauxTva', 'tauxRetenuSource'],
        )
        self.assertEqual(
            [child.text for child in node],
            ['1000003', 'FAC-RAS-001', '2026-06-20', '2026-06-02', '3', '1000.00', '20', '75'],
        )

    def test_withholding_entry_is_not_a_payment(self):
        bill = self._create_bill()
        self._pay_net(bill)
        self._book_withholding(bill)
        self.assertEqual(bill.payment_state, 'paid')
        decl = self._generated()
        self.assertEqual(len(decl.line_ids), 1)
        self.assertEqual(decl.line_ids.montant_ht, 1000.0)

    def test_partial_payment_prorated_on_net_amount(self):
        bill = self._create_bill()
        # Net à payer 1050 (1200 - 150) : 525 réglés = la moitié de la facture
        self._pay(bill, date(2026, 6, 10), 525.0)
        self._pay(bill, date(2026, 7, 10), 525.0)
        june = self._generated('6')
        self.assertEqual(june.line_ids.montant_ht, 500.0)
        self.assertEqual(june.line_ids.montant_retenu, 75.0)
        july = self._generated('7')
        self.assertEqual(july.line_ids.montant_ht, 500.0)

    def test_supplier_not_subject_excluded_unless_forced_on_bill(self):
        other = self.env['res.partner'].create({'name': 'Hors RAS', 'is_company': True, 'vat': '2000004'})
        bill = self._create_bill(partner=other, post=False)
        self.assertFalse(bill.l10n_ma_ras_applicable)
        bill.action_post()
        self._pay(bill, date(2026, 6, 20), bill.amount_total)
        june = self._generated()
        self.assertFalse(june.line_ids)

        forced = self._create_bill(partner=other, post=False)
        forced.l10n_ma_ras_applicable = True
        forced.l10n_ma_ras_taux = '75'
        forced.action_post()
        self._pay_net(forced)
        june.action_generate_lines()
        self.assertEqual(june.line_ids.move_id, forced)

    def test_nature_by_line_and_goods_excluded(self):
        asset_account = self.env['account.account'].create({
            'code': '233100', 'name': 'Matériel RAS', 'account_type': 'asset_fixed',
        })
        bill = self._create_bill(lines=[
            (self.service, 1000.0, self.tax_20),
            (self.works, 2000.0, self.tax_20),
            (self.goods, 500.0, self.tax_20),
            (False, 300.0, self.tax_10),
        ], post=False)
        goods_line = bill.invoice_line_ids.filtered(lambda l: l.product_id == self.goods)
        self.assertFalse(goods_line.l10n_ma_ras_nature)
        bill.invoice_line_ids.filtered(lambda l: not l.product_id).account_id = asset_account
        self.assertEqual(bill.invoice_line_ids.filtered(lambda l: not l.product_id).l10n_ma_ras_nature, '1')
        bill.action_post()
        self._pay_net(bill)
        decl = self._generated()
        amounts = {(l.ref_nat_opt, l.taux_tva): l.montant_ht for l in decl.line_ids}
        self.assertEqual(amounts, {('1', 10.0): 300.0, ('2', 20.0): 2000.0, ('3', 20.0): 1000.0})

    def test_line_without_product_uses_company_default(self):
        bill = self._create_bill(lines=[(False, 1000.0, self.tax_20)])
        self.assertEqual(bill.invoice_line_ids.l10n_ma_ras_nature, '3')

    def test_individual_supplier_defaults_to_100(self):
        person = self.env['res.partner'].create({
            'name': 'Consultant indépendant', 'is_company': False,
            'vat': '3000005', 'l10n_ma_ras_applicable': True,
        })
        self.assertEqual(person.l10n_ma_ras_taux, '100')
        bill = self._create_bill(partner=person)
        self.assertEqual(bill.l10n_ma_ras_amount, 200.0)

    def test_vat_rate_checked_against_operation_year(self):
        bill = self._create_bill(lines=[(self.service, 1000.0, self.tax_14)])
        self._pay_net(bill)
        decl = self._generated()
        with self.assertRaises(UserError) as ctx:
            decl.action_validate()
        self.assertIn("taux de TVA 14 % non autorisé pour une opération de 2026", str(ctx.exception))

    def test_transitional_rate_of_2025_accepted(self):
        bill = self._create_bill(lines=[(self.service, 1000.0, self.tax_18)], invoice_date=date(2025, 12, 15))
        self._pay_net(bill, date(2026, 1, 10))
        decl = self._generated('1')
        decl.action_validate()
        root = etree.fromstring(decl._generate_xml_content())
        self.assertEqual(root.findtext('./fournisseurs/fournisseur/tauxTva'), '18')

    def test_missing_ifu_blocked_then_refreshed(self):
        partner = self.env['res.partner'].create({
            'name': 'Sans IF', 'is_company': True, 'l10n_ma_ras_applicable': True,
        })
        bill = self._create_bill(partner=partner)
        self._pay_net(bill)
        decl = self._generated()
        self.assertTrue(decl.line_ids.missing_supplier_data)
        partner.vat = '4000006'
        action = decl.line_ids.action_open_partner()
        partner.with_context(action['context']).action_l10n_ma_rd_save_and_refresh()
        self.assertEqual(decl.line_ids.ifu_fournisseur, '4000006')
        decl.action_validate()
        self.assertEqual(decl.state, 'validated')

    def test_payment_declared_once_but_independent_from_rd(self):
        bill = self._create_bill()
        self._pay_net(bill)
        custom = self._create_declaration(
            periode='6', periodicite='custom', date_from=date(2026, 5, 1), date_to=date(2026, 6, 30),
        )
        custom.action_generate_lines()
        self.assertEqual(len(custom.line_ids), 1)
        july = self._create_declaration(
            periode='7', periodicite='custom', date_from=date(2026, 6, 1), date_to=date(2026, 7, 31),
        )
        july.action_generate_lines()
        self.assertFalse(july.line_ids, "Paiement déjà repris dans la RAS de juin")

        # Le relevé des déductions est une déclaration distincte : le même paiement y figure
        rd = self.env['tva.rd.declaration'].create({
            'name': 'RD 06/2026', 'company_id': self.company.id, 'annee': '2026',
            'periodicite': 'monthly', 'periode': '6',
        })
        rd.action_generate_lines()
        self.assertEqual(rd.line_ids.move_id, bill)

    def test_ras_is_monthly_only(self):
        wizard = self.env['tva.rd.wizard'].create({
            'declaration_type': 'ras', 'annee': '2026', 'periodicite': 'quarterly',
            'periode_trimestrielle': 'T2', 'company_id': self.company.id,
        })
        with self.assertRaises(UserError):
            wizard.action_create_declaration()

    def test_wizard_creates_ras_declaration(self):
        bill = self._create_bill()
        self._pay_net(bill)
        wizard = self.env['tva.rd.wizard'].create({
            'declaration_type': 'ras', 'annee': '2026', 'periodicite': 'monthly',
            'periode_mensuelle': '6', 'company_id': self.company.id,
        })
        action = wizard.action_create_declaration()
        self.assertEqual(action['res_model'], 'tva.ras.declaration')
        decl = self.env['tva.ras.declaration'].browse(action['res_id'])
        self.assertEqual(decl.name, 'RAS TVA 06/2026')
        self.assertEqual(decl.line_ids.move_id, bill)

    def test_locked_after_validation(self):
        bill = self._create_bill()
        self._pay_net(bill)
        decl = self._generated()
        decl.action_validate()
        with self.assertRaises(UserError):
            decl.line_ids.montant_ht = 1.0
        decl.action_export_zip()
        self.assertEqual(decl.state, 'exported')
        self.assertTrue(self.env['ir.attachment'].search([
            ('res_model', '=', 'tva.ras.declaration'), ('res_id', '=', decl.id), ('name', '=like', 'RAS_TVA_%.zip'),
        ]))
