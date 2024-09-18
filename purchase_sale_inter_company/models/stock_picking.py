# Copyright 2018 Tecnativa - Carlos Dauden
# Copyright 2018 Tecnativa - Pedro M. Baeza
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import logging

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    intercompany_picking_id = fields.Many2one(comodel_name="stock.picking", copy=False)
    intercompany_return_picking_id = fields.Many2one(
        comodel_name="stock.picking", copy=False
    )

    @api.depends("intercompany_picking_id.state")
    def _compute_state(self):
        """
        If the picking is inter-company, it's an 'incoming'
        type of picking, and it has not been validated nor canceled
        we compute it's state based on the other picking state
        """
        res = super()._compute_state()
        for picking in self:
            if (
                picking.company_id.sync_picking_state
                and picking.intercompany_picking_id
                and picking.picking_type_code == "incoming"
                and picking.state not in ["done", "cancel"]
            ):
                if picking.intercompany_picking_id.state in ["confirmed", "assigned"]:
                    picking.state = "waiting"
                else:
                    picking.state = picking.intercompany_picking_id.state

        return res

    def _warn_move_line_mismatch(self, ic_pick, product, ml, po_ml):
        self.ensure_one()
        note = _(
            "Mismatch between move lines (%s vs %s) with the "
            "corresponding PO picking %s for assigning "
            "quantities and lots from %s for product %s"
        ) % (len(ml), len(po_ml), ic_pick.name, self.name, product.name)
        _logger.warning(note)
        self.activity_schedule(
            "mail.mail_activity_data_warning",
            fields.Date.today(),
            note=note,
            # Try to notify someone relevant
            user_id=(
                self.sale_id.user_id.id
                or self.sale_id.team_id.user_id.id
                or SUPERUSER_ID,
            ),
        )

    def _sync_lots(self, ml, po_ml):
        lot_id = ml.lot_id
        if not lot_id:
            return
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

    def _action_done(self):
        ret = super()._action_done()

        # sync lots for move lines on returns
        # TODO: integrate with the non-return part just below this clause
        for pick in self.filtered(lambda x: x.intercompany_return_picking_id).sudo():
            ic_pick = pick.intercompany_return_picking_id
            dest_company = ic_pick.sudo().company_id
            if not dest_company.sync_picking:
                continue
            ic_user = dest_company.intercompany_sale_user_id
            ic_pick = ic_pick.with_user(ic_user)
            try:
                dest_move_qty_update_dict = {}
                for move in pick.move_lines:
                    product = move.product_id
                    move_lines = move.move_line_ids
                    po_move = ic_pick.move_lines.filtered(
                        lambda x, prod=product: x.product_id == prod
                    )
                    po_move_lines = po_move.mapped("move_line_ids")
                    # Don't support one move splitting into multiple moves with the same product
                    # (Not sure how to even achieve that in Odoo)
                    # And don't support cases with more stock.move.line SO-side than PO side
                    if len(po_move) != 1 or len(move_lines) > len(po_move_lines):
                        pick._warn_move_line_mismatch(
                            ic_pick, product, move_lines, po_move_lines
                        )
                        continue
                    # Delete excess stock.move.line on PO side
                    if len(po_move_lines) > len(move_lines):
                        po_move_lines[len(move_lines) :].unlink()
                    for ml, po_ml in zip(move_lines, po_move_lines):
                        pick._sync_lots(ml, po_ml)
                        po_ml.qty_done = ml.qty_done
                    dest_move_qty_update_dict.setdefault(po_move, 0.0)
                    dest_move_qty_update_dict[po_move] += move.quantity_done
                for dest_move, qty_done in dest_move_qty_update_dict.items():
                    dest_move.quantity_done = qty_done
                ic_pick._action_done()
            except Exception as e:
                if dest_company.sync_picking_failure_action == "raise":
                    raise
                else:
                    pick._notify_picking_problem(
                        pick.sale_id.auto_purchase_order_id,
                        additional_note=str(e),
                    )

        # sync lots for move lines on pickings
        for pick in self.filtered(
            lambda x: x.location_dest_id.usage == "customer"
        ).sudo():
            purchase = pick.sale_id.auto_purchase_order_id
            if not purchase:
                continue
            dest_company = purchase.company_id
            if not dest_company.sync_picking:
                continue
            try:
                if not purchase.picking_ids:
                    raise UserError(_("PO does not exist or has no receipts"))
                ic_user = dest_company.intercompany_sale_user_id
                purchase.picking_ids.write({"intercompany_picking_id": pick.id})
                ic_pick = pick.intercompany_picking_id.with_user(ic_user)
                if not ic_pick and purchase.picking_ids[0]:
                    pick.write({"intercompany_picking_id": purchase.picking_ids[0]})
                # formerly in action_done_intercompany_actions
                dest_move_qty_update_dict = {}
                for move in pick.move_lines:
                    move_lines = move.move_line_ids
                    po_move = move.sale_line_id.auto_purchase_line_id.move_ids.filtered(
                        lambda x, ic_picking=ic_pick, prod=move.product_id: x.picking_id  # noqa
                        == ic_picking
                        and x.product_id == prod
                    )
                    po_move_lines = po_move.mapped("move_line_ids")
                    # Don't support one move splitting into multiple moves with the same product
                    # (Not sure how to even achieve that in Odoo)
                    # And don't support cases with more stock.move.line SO-side than PO side
                    if len(po_move) != 1 or len(move_lines) > len(po_move_lines):
                        self._notify_picking_problem(
                            purchase,
                            additional_note=_(
                                "Mismatch between move lines with the "
                                "corresponding PO %s for assigning "
                                "quantities and lots from %s for product %s"
                            )
                            % (purchase.name, pick.name, move.product_id.name),
                        )
                        continue
                    # Delete excess stock.move.line on PO side
                    if len(po_move_lines) > len(move_lines):
                        po_move_lines[len(move_lines) :].unlink()
                    for ml, po_ml in zip(move_lines, po_move_lines):
                        pick._sync_lots(ml, po_ml)
                        po_ml.qty_done = ml.qty_done
                    dest_move_qty_update_dict.setdefault(po_move, 0.0)
                    dest_move_qty_update_dict[po_move] += move.quantity_done
                # formerly in sync_receipt_to_delivery
                # "No backorder" case splits SO moves in two while PO stays the same.
                # Aggregating writes per each PO move makes sure qty does not get overwritten
                for dest_move, qty_done in dest_move_qty_update_dict.items():
                    dest_move.quantity_done = qty_done
                ic_pick.with_context(
                    cancel_backorder=bool(
                        self.env.context.get("picking_ids_not_to_backorder")
                    )
                )._action_done()

            except Exception as e:
                if dest_company.sync_picking_failure_action == "raise":
                    raise
                else:
                    pick._notify_picking_problem(
                        pick.sale_id.auto_purchase_order_id,
                        additional_note=str(e),
                    )

        return ret

    def _notify_picking_problem(self, purchase, additional_note=False):
        self.ensure_one()
        note = _(
            "Failure to confirm picking for PO %s. "
            "Original picking %s still confirmed, please check "
            "the other side manually."
        ) % (purchase.name, self.name)
        if additional_note:
            note += _(" Additional info: ") + additional_note
        _logger.warning(note)
        user_id = self.sudo()._get_user_to_notify(purchase)
        self.sudo().activity_schedule(
            "mail.mail_activity_data_warning",
            fields.Date.today(),
            note=note,
            user_id=user_id or SUPERUSER_ID,
        )

    def _get_user_to_notify(self, purchase):
        """Notify user based on res.config.settings"""
        if purchase.company_id.notification_side == "so":
            return (
                self.company_id.notify_user_id.id
                or self.sale_id.user_id.id
                or self.sale_id.team_id.user_id.id
            )
        return purchase.company_id.notify_user_id.id or purchase.user_id.id

    def button_validate(self):
        res = super().button_validate()

        # if the flag is set, block the validation of the picking in the destination company
        if self.env.company.block_po_manual_picking_validation:
            for record in self:
                dest_company = record.partner_id.commercial_partner_id.ref_company_ids
                if (
                    dest_company and record.picking_type_code == "incoming"
                ) and record.state in ["done", "waiting", "assigned"]:
                    raise UserError(
                        _(
                            "Manual validation of the picking is not allowed"
                            " in the destination company."
                        )
                    )
        return res

    def _update_extra_data_in_picking(self, picking):
        if hasattr(self, "_cal_weight"):  # from delivery module
            self._cal_weight()
