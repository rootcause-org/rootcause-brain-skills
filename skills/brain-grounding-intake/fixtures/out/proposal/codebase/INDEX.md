---
description: Routes a customer symptom to the part of the SalonX codebase that grounds the answer. Two apps live side by side, the CodeIgniter app under legacy/ and the Laravel app under app/.
---

# SalonX codebase map

The customer sees one product. The code is two apps behind one entry point.

- Online booking, free slots, cancellations: read codebase/booking.md.
- Reminders, confirmations, any mail the patient receives: read codebase/booking.md first, then
  ask about the sender, we could not tell which of the two is live.
- Anything a salon sees as data (appointments, tickets, settings): read databases/staging.md.
- Invoices and exports: nothing in this codebase grounds them yet, ask the dev first.

Both apps talk to the same database. Table names are plural, model class names are singular:
`agenda.status` is read by `/mirrors/app/app/app/Models/Booking.php` and by
`/mirrors/app/legacy/application/models/Booking_model.php`.

(?) The legacy entry point `/mirrors/app/legacy/index.php` still serves most URLs.
