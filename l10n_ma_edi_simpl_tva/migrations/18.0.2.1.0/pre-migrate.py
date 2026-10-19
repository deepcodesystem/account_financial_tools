import logging

_logger = logging.getLogger(__name__)


def _column_exists(cr, table, column):
    cr.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return bool(cr.fetchone())


def _report_conflicts(cr, column, target, label):
    cr.execute(f"""
        SELECT id, name, {target}, {column} FROM res_partner
         WHERE {column} IS NOT NULL AND {column} != ''
           AND {target} IS NOT NULL AND {target} != ''
           AND regexp_replace(upper({target}), '^MA|[\\s.-]', '', 'g') != {column}
    """)
    for partner_id, name, kept, dropped in cr.fetchall():
        _logger.warning(
            "Contact %s (%s) : %s conservé %r, ancienne valeur EDI %r abandonnée.",
            partner_id, name, label, kept, dropped,
        )


def migrate(cr, version):
    """IF / ICE EDI : reprise des anciens champs dédiés dans le N° TVA (IF) et le champ ICE
    de l10n_ma (company_registry), sans écraser une valeur déjà saisie."""
    if not version:
        return
    if _column_exists(cr, 'res_partner', 'l10n_ma_rd_if'):
        _report_conflicts(cr, 'l10n_ma_rd_if', 'vat', 'IF (N° TVA)')
        cr.execute("""
            UPDATE res_partner SET vat = l10n_ma_rd_if
             WHERE l10n_ma_rd_if IS NOT NULL AND l10n_ma_rd_if != '' AND COALESCE(vat, '') = ''
        """)
        _logger.info("IF EDI repris dans le N° TVA de %s contacts.", cr.rowcount)
    if _column_exists(cr, 'res_partner', 'l10n_ma_rd_ice'):
        _report_conflicts(cr, 'l10n_ma_rd_ice', 'company_registry', 'ICE')
        cr.execute("""
            UPDATE res_partner SET company_registry = l10n_ma_rd_ice
             WHERE l10n_ma_rd_ice IS NOT NULL AND l10n_ma_rd_ice != '' AND COALESCE(company_registry, '') = ''
        """)
        _logger.info("ICE EDI repris dans le champ ICE de %s contacts.", cr.rowcount)
    for column, target in (('l10n_ma_rd_identifiant_fiscal', 'vat'), ('l10n_ma_rd_ice', 'company_registry')):
        if _column_exists(cr, 'res_company', column):
            cr.execute(f"""
                UPDATE res_partner p SET {target} = c.{column}
                  FROM res_company c
                 WHERE c.partner_id = p.id AND c.{column} IS NOT NULL AND c.{column} != ''
                   AND COALESCE(p.{target}, '') = ''
            """)
            _logger.info("Société : %s repris dans %s pour %s société(s).", column, target, cr.rowcount)
