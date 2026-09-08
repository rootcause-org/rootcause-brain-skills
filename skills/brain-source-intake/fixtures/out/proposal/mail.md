---
description: Which code sends mail to a patient (reminders, confirmations) in SalonX, and which of the two senders is still live, for questions about a mail that did not arrive.
---

# Mail to the patient

Two senders exist and we could not tell which one is live.

- `/mirrors/app/app/app/Mail/ReminderMail.php` is the Laravel mailable.
- `/mirrors/app/legacy/application/libraries/Mailer.php` is the CodeIgniter library.
- Transport config sits in `/mirrors/app/app/config/mail.php`.

The reminder is scheduled, not sent from a screen. Two schedulers exist:
`/mirrors/app/app/app/Jobs/SendReminder.php` and
`/mirrors/app/legacy/application/controllers/cron/Reminders.php`.

(?) Send time is not a per salon setting, so "can it be later" is a product question.
