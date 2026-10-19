-- Renommage du module l10n_ma_edi_releve_deductions en l10n_ma_edi_simpl_tva
-- sur une base où l'ancien module est installé (données conservées).
--
-- 1. Sauvegarder la base et arrêter Odoo.
-- 2. Déployer le dossier l10n_ma_edi_simpl_tva et supprimer l10n_ma_edi_releve_deductions.
-- 3. Exécuter ce script : psql -d <base> -f rename_from_l10n_ma_edi_releve_deductions.sql
-- 4. Mettre à jour : odoo-bin -c odoo.conf -d <base> -u l10n_ma_edi_simpl_tva --stop-after-init
BEGIN;
UPDATE ir_module_module SET name = 'l10n_ma_edi_simpl_tva'
 WHERE name = 'l10n_ma_edi_releve_deductions';
UPDATE ir_model_data SET module = 'l10n_ma_edi_simpl_tva'
 WHERE module = 'l10n_ma_edi_releve_deductions';
UPDATE ir_model_data SET name = 'module_l10n_ma_edi_simpl_tva'
 WHERE module = 'base' AND model = 'ir.module.module'
   AND name = 'module_l10n_ma_edi_releve_deductions';
UPDATE ir_module_module_dependency SET name = 'l10n_ma_edi_simpl_tva'
 WHERE name = 'l10n_ma_edi_releve_deductions';
COMMIT;
