# schema staging (mysql 5.6.51-log)

database salonx_staging · 6 base tables · 35 columns · 2 foreign key columns · views 1 · collected 2026-09-08T06:12:00+00:00
tenant candidates: user_id (6 tables) · agenda_id (2 tables) · customer_id (1 tables)
tables without user_id (1): users

Per table: rows · size · role · tenant column · flags. Roles are a guess from names, indexes and counts, not a contract.

logging · 900000 rows · 340.0 MB · log · user_id · ts:created_at
agenda · 184000 rows · 52.0 MB · core · user_id · soft-delete:deleted · ts:created_at · fk-out:1 fk-in:1 · enum:status=open (70%), done (25%), cancelled (5%) · enum:deleted=0 (98%), 1 (2%)
remarks · 91755 exact rows · 26.0 MB · core · user_id · ts:time_added · fk-out:1 fk-in:0 · enum:state=paid (90%), open (10%) · comment:POS ticket header, not a free text note
reminder_jobs · 11840 exact rows · 3.0 MB · log · user_id · ts:created_at
settings · 520 exact rows · 0.2 MB · settings · user_id · ts:created_at · enum:reminder_enabled=1 (90%), 0 (10%)
users · 520 rows · 0.3 MB · core · no tenant col · soft-delete:active · ts:created_at · fk-out:0 fk-in:1 · comment:salon accounts, one row per salon

views (1): v_agenda_today

budget: 41 queries in 18.4s, 1 skipped
skipped logging.message: OperationalError: query timed out
