# Copyright 2018 Tecnativa - Carlos Dauden
# Copyright 2018 Tecnativa - Pedro M. Baeza
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, api, fields, models
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
            for move_line in pick.move_line_ids:
                sale_line_id = move_line.move_id.sale_line_id
                po_move_lines = sale_line_id.auto_purchase_line_id.move_ids.mapped(
                    "move_line_ids"
                )
                if not po_move_lines:
                    raise UserError(
                        _(
                            "There's no corresponding line in PO %(purchase)s for assigning "
                            "qty from %(picking)s for product %(product)s"
                        )
                        % {
                            "purchase": purchase.name,
                            "picking": pick.name,
                            "product": move_line.product_id.name,
                        }
                    )
        return super()._action_done()

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

    @api.model
    def _prepare_picking_line_data(self, src_picking, dest_picking):
        self.ensure_one()
        if self.check_all_done(src_picking):
            for line in src_picking.sudo().move_ids_without_package:
                line.write({"quantity_done": line.reserved_availability})
        for src_line in src_picking.sudo().move_ids_without_package:
            if (
                src_line.product_id
                in dest_picking.sudo().move_ids_without_package.mapped("product_id")
                and src_line.quantity_done > 0
            ):
                dest_move = dest_picking.sudo().move_ids_without_package.filtered(
                    lambda m: m.product_id == src_line.product_id
                )
                dest_move.write(
                    {"quantity_done": dest_move.quantity_done + src_line.quantity_done}
                )

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
