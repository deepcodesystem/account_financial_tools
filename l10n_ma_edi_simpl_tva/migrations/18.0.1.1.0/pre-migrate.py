def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        ALTER TABLE tva_rd_declaration
        ADD COLUMN IF NOT EXISTS regime_comptabilite varchar
    """)
    cr.execute("""
        UPDATE tva_rd_declaration
        SET regime_comptabilite = COALESCE(NULLIF(regime, ''), '1')
    """)
    cr.execute("""
        UPDATE tva_rd_declaration
        SET regime = CASE WHEN periodicite = 'quarterly' THEN '2' ELSE '1' END
    """)
