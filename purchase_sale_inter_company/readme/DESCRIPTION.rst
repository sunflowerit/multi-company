This module is useful if there are multiple companies in the same Odoo database and those companies sell goods or services among themselves.
It allows to create a sale order in company A from a purchase order in company B.

Imagine you have company A and company B in the same Odoo database:

* Company A purchase goods from company B.
* Company A will create a purchase order with company B as supplier.
* This module automate the creation of the sale order in company B with company A as customer.

It allows to create a sale order in company A from a purchase order in company B, and keep delivery/receipt pickings synced, including backorders.

When Company A sends a product tracked by lot or serial number, a new lot/serial number with the same name is created in Company B to match it, if one doesn't already exist.
