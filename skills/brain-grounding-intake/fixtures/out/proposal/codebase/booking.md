---
description: Where online booking, slot visibility and cancellation state are decided in the SalonX code, for questions about a customer who cannot book or sees the wrong agenda.
---

# Booking and slot visibility

Two models read the same `agenda` table.

- `/mirrors/app/app/app/Models/Booking.php` is the Laravel model, used by the newer screens.
- `/mirrors/app/legacy/application/models/Booking_model.php` is the CodeIgniter model.
- The controller a patient hits is `/mirrors/app/legacy/application/controllers/Booking.php`.

Slot visibility is decided on the model, not in a setting, so a salon cannot change it from the
product. Cancellation is a state on the booking row, not a delete.

(?) The Laravel routes in `/mirrors/app/app/routes/web.php` cover only the newer screens.
