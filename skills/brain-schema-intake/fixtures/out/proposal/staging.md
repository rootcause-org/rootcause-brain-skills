---
description: What lives where in the salonx staging database: the tenant key, the core entities, the settings rows and the reminder queue, for grounding a support answer in data.
---

# The salonx database

Tenant key: `user_id` on every table but `users`. `users` is one row per salon (?).
Never answer across salons: always filter on `user_id`.

## Core entities

- `agenda` is the appointment. `agenda.status` is open, done or cancelled, and
  `agenda.deleted` is a soft delete flag, so a cancelled appointment still has a row.
- `remarks` is the point of sale ticket header, not a note (?). `remarks.total` is the amount,
  `remarks.state` is paid or open, and `remarks.agenda_id` links it back to the appointment.
- `users` holds the salon account. `users.active` marks a closed account.

## Settings

`settings` is one row per salon: the unique index on `user_id` proves it. `settings.reminder_time`
is the hour the reminder leaves (?) and `settings.reminder_enabled` is the on switch.

## Queue and logs

`reminder_jobs` is one row per planned reminder. `already_sent` flips when it left and `sent_at`
carries the moment (?). `logging` is a write only trail, 900k rows, so never scan it without a
`user_id` filter and a date bound.

## Reading rules

- Timestamps: `agenda` uses `created_at`, `remarks` uses `time_added`, a legacy hint (?).
- Anything the customer sees as removed is a flag, not a missing row.
