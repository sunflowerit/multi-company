import logging

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ReturnPicking(models.TransientModel):
    _inherit = "stock.return.picking"

    def create_returns(self):
        res = super(ReturnPicking, self).create_returns()
        pick = self.picking_id

        # dont trigger on returns of incoming pickings
        if pick.picking_type_code == "incoming":
            return res

        # only trigger in case there is a coupled picking on the other side
        ic_pick = pick.intercompany_picking_id
        if not ic_pick:
            return res
        dest_company = ic_pick.sudo().company_id
        intercompany_user = dest_company.intercompany_sale_user_id
        ic_pick = ic_pick.with_user(intercompany_user)

        # warn in case of partial return
        total_qty_sent = sum(pick.move_line_ids.mapped("product_uom_qty"))
        total_qty_returned = sum(self.product_return_moves.mapped("quantity"))
        if total_qty_returned < total_qty_sent:
            note = _(
                "This inter-company shipment was partially returned, but also has a "
                "counterpart in company {}: {}. This could not be automatically returned; "
                "please take care to do this manually."
            ).format(ic_pick.company_id.name, ic_pick.name, pick.name)
            pick._notify_picking_problem(
                pick.sale_id.auto_purchase_order_id, additional_note=note
            )
            return res

        return_pick_id = res.get("res_id")
        if not return_pick_id:
            return res
        return_pick = self.env["stock.picking"].browse(return_pick_id)

        # create a return for the IC pick as well
        intercompany_user = dest_company.intercompany_sale_user_id
        vals = {"picking_id": ic_pick.id, "location_id": pick.location_id.id}
        return_wizard = (
            self.env["stock.return.picking"]
            .with_context(active_id=ic_pick.id)
            .with_user(intercompany_user)
            .create(vals)
        )
        return_wizard._onchange_picking_id()
        try:
            action = return_wizard.create_returns()
        except UserError:
            note = _(
                "This inter-company shipment was returned, but also has a "
                "counterpart in company {}: {}. This could not be automatically returned; "
                "please take care to do this manually."
            ).format(ic_pick.company_id.name, ic_pick.name, pick.name)
            pick._notify_picking_problem(
                pick.sale_id.auto_purchase_order_id, additional_note=note
            )
            return res

        ic_return_pick_id = action["res_id"]
        return_pick.intercompany_return_picking_id = ic_return_pick_id

        return res
