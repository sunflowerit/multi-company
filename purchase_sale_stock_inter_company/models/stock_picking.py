# Copyright 2018 Tecnativa - Carlos Dauden
# Copyright 2018 Tecnativa - Pedro M. Baeza
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = "stock.picking"

    intercompany_picking_id = fields.Many2one(comodel_name="stock.picking", copy=False)

    def _action_done(self):
        for pick in self.filtered(
            lambda x: x.location_dest_id.usage == "customer"
        ).sudo():
            purchase = pick.sale_id.auto_purchase_order_id
            if not purchase:
                continue
            purchase.picking_ids.write({"intercompany_picking_id": pick.id})
            if not pick.intercompany_picking_id and purchase.picking_ids[0]:
                pick.write({"intercompany_picking_id": purchase.picking_ids[0]})
            pick._action_done_intercompany_actions(purchase)
        return super()._action_done()

    def _action_done_intercompany_actions(self, purchase):
        self.ensure_one()
        try:
            pick = self
            for move in pick.move_lines:
                move_lines = move.move_line_ids
                po_move_lines = (
                    move.sale_line_id.auto_purchase_line_id.move_ids.filtered(
                        lambda x, ic_pick=pick.intercompany_picking_id: x.picking_id
                        == ic_pick
                    ).mapped("move_line_ids")
                )
                if not len(move_lines) == len(po_move_lines):
                    note = _(
                        "Mismatch between move lines with the "
                        "corresponding PO %s for assigning "
                        "quantities and lots from %s for product %s"
                    ) % (purchase.name, pick.name, move.product_id.name)
                    # Configurable parameter so we don't lock the picking validation
                    if (
                        not self.env["ir.config_parameter"]
                        .sudo()
                        .get_param("purchase_sale_inter_company.soft_picking_mismatch")
                    ):
                        raise UserError(note)
                    self.activity_schedule(
                        "mail.mail_activity_data_warning",
                        fields.Date.today(),
                        note=note,
                        # Try to notify someone relevant
                        user_id=(
                            pick.sale_id.user_id.id
                            or pick.sale_id.team_id.user_id.id
                            or SUPERUSER_ID,
                        ),
                    )
                # check and assign lots here
                for ml, po_ml in zip(move_lines, po_move_lines):
                    lot_id = ml.lot_id
                    if not lot_id:
                        continue
                    # search if the same lot exists in destination company
                    dest_lot_id = (
                        self.env["stock.production.lot"]
                        .sudo()
                        .search(
                            [
                                ("product_id", "=", lot_id.product_id.id),
                                ("name", "=", lot_id.name),
                                ("company_id", "=", po_ml.company_id.id),
                            ],
                            limit=1,
                        )
                    )
                    if not dest_lot_id:
                        # if it doesn't exist, create it by copying from original company
                        dest_lot_id = lot_id.copy({"company_id": po_ml.company_id.id})
                    po_ml.lot_id = dest_lot_id

        except Exception:
            if self.env.company_id.sync_picking_failure_action == "raise":
                raise
            else:
                self._notify_picking_problem(purchase)

    def _notify_picking_problem(self, purchase):
        self.ensure_one()
        note = _(
            "Failure to confirm picking for PO %(purchase)s. "
            "Original picking %(stock)s still confirmed, please check "
            "the other side manually."
        ) % {"purchase": purchase.name, "stock": self.name}
        self.activity_schedule(
            "mail.mail_activity_data_warning",
            fields.Date.today(),
            note=note,
            # Try to notify someone relevant
            user_id=(
                self.company_id.notify_user_id.id
                or self.sale_id.user_id.id
                or self.sale_id.team_id.user_id.id
                or SUPERUSER_ID,
            ),
        )

    def button_validate(self):
        res = super().button_validate()
        for record in self.sudo():
            dest_company = record.partner_id.commercial_partner_id.ref_company_ids
            if (
                dest_company
                and dest_company.sync_picking
                and record.state == "done"
                and record.picking_type_code == "outgoing"
            ):
                if record.intercompany_picking_id:
                    record._sync_receipt_with_delivery(
                        dest_company,
                        record.sale_id,
                    )
        return res

    def _sync_receipt_with_delivery(self, dest_company, sale_order):
        self.ensure_one()
        intercompany_user = dest_company.intercompany_sale_user_id
        purchase_order = sale_order.auto_purchase_order_id.sudo()
        if not (purchase_order and purchase_order.picking_ids):
            raise UserError(_("PO does not exist or has no receipts"))
        if self.intercompany_picking_id:
            dest_picking = self.intercompany_picking_id.with_user(intercompany_user.id)
            for picking_line in self.move_line_ids_without_package.sorted("qty_done"):
                dest_picking_line = (
                    dest_picking.sudo().move_line_ids_without_package.filtered(
                        lambda l: l.product_id.id == picking_line.product_id.id
                    )
                )
                dest_picking_line.sudo().write(
                    {
                        "qty_done": picking_line.qty_done,
                    }
                )
            for picking_move in self.move_ids_without_package.sorted("quantity_done"):
                dest_picking_move = (
                    dest_picking.sudo().move_ids_without_package.filtered(
                        lambda l: l.product_id.id == picking_move.product_id.id
                    )
                )
                dest_picking_move.sudo().write(
                    {
                        "quantity_done": picking_move.quantity_done,
                    }
                )
            dest_picking.sudo().with_context(
                cancel_backorder=bool(
                    self.env.context.get("picking_ids_not_to_backorder")
                )
            )._action_done()

    def _update_extra_data_in_picking(self, picking):
        if hasattr(self, "_cal_weight"):  # from delivery module
            self._cal_weight()
