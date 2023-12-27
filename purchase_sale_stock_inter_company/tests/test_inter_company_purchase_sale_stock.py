# Copyright 2013-Today Odoo SA
# Copyright 2019-2019 Chafique DELLI @ Akretion
# Copyright 2018-2019 Tecnativa - Carlos Dauden
# Copyright 2020 ForgeFlow S.L. (https://www.forgeflow.com)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import re

from odoo.tests.common import Form

from odoo.addons.purchase_sale_inter_company.tests.test_inter_company_purchase_sale import (
    TestPurchaseSaleInterCompany,
)


class TestPurchaseSaleStockInterCompany(TestPurchaseSaleInterCompany):
    @classmethod
    def _create_warehouse(cls, code, company):
        address = cls.env["res.partner"].create({"name": f"{code} address"})
        return cls.env["stock.warehouse"].create(
            {
                "name": f"Warehouse {code}",
                "code": code,
                "partner_id": address.id,
                "company_id": company.id,
            }
        )
    
    @classmethod
    def _create_serial_and_quant(cls, product, name, company):
        lot = cls.lot_obj.create(
            {"product_id": product.id, "name": name, "company_id": company.id}
        )
        cls.quant_obj.create(
            {
                "product_id": product.id,
                "location_id": cls.warehouse_a.lot_stock_id.id,
                "quantity": 1,
                "lot_id": lot.id,
            }
        )
        return lot

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.lot_obj = cls.env["stock.production.lot"]
        cls.quant_obj = cls.env["stock.quant"]
        # Configure 2 Warehouse per company
        cls.warehouse_a = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company_a.id)]
        )
        cls.warehouse_b = cls._create_warehouse("CA-WB", cls.company_a)

        cls.warehouse_c = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company_b.id)]
        )
        cls.warehouse_d = cls._create_warehouse("CB-WD", cls.company_b)
        cls.company_b.warehouse_id = cls.warehouse_c
        cls.consumable_product_2 = cls.env["product.product"].create(
            {
                "name": "Consumable Product 2",
                "type": "consu",
            }
        )
        cls.stockable_product_serial = cls.env["product.product"].create(
            {
                "name": "Stockable Product Tracked by Serial",
                "type": "product",
                "tracking": "serial",
                "categ_id": cls.env.ref("product.product_category_all").id,
            }
        )
                # Add quants for product tracked by serial to supplier
        cls.serial_1 = cls._create_serial_and_quant(
            cls.stockable_product_serial, "111", cls.company_b
        )
        cls.serial_2 = cls._create_serial_and_quant(
            cls.stockable_product_serial, "222", cls.company_b
        )
        cls.serial_3 = cls._create_serial_and_quant(
            cls.stockable_product_serial, "333", cls.company_b
        )

    def test_deliver_to_warehouse_a(self):
        self.purchase_company_a.picking_type_id = self.warehouse_a.in_type_id
        sale = self._approve_po(self.purchase_company_a)
        self.assertEqual(self.warehouse_a.partner_id, sale.partner_shipping_id)

    def test_deliver_to_warehouse_b(self):
        self.purchase_company_a.picking_type_id = self.warehouse_b.in_type_id
        sale = self._approve_po(self.purchase_company_a)
        self.assertEqual(self.warehouse_b.partner_id, sale.partner_shipping_id)

    def test_send_from_warehouse_c(self):
        self.company_b.warehouse_id = self.warehouse_c
        sale = self._approve_po(self.purchase_company_a)
        self.assertEqual(sale.warehouse_id, self.warehouse_c)

    def test_send_from_warehouse_d(self):
        self.company_b.warehouse_id = self.warehouse_d
        sale = self._approve_po(self.purchase_company_a)
        self.assertEqual(sale.warehouse_id, self.warehouse_d)

    def test_purchase_sale_stock_inter_company(self):
        self.purchase_company_a.notes = "Test note"
        sale = self._approve_po(self.purchase_company_a)
        self.assertEqual(
            sale.partner_shipping_id,
            self.purchase_company_a.picking_type_id.warehouse_id.partner_id,
        )
        self.assertEqual(sale.warehouse_id, self.warehouse_c)

    def test_confirm_several_picking(self):
        """
        Ensure that confirming several picking is not broken
        """
        purchase_1 = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        purchase_2 = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        sale_1 = self._approve_po(purchase_1)
        sale_2 = self._approve_po(purchase_2)
        pickings = sale_1.picking_ids | sale_2.picking_ids
        for move in pickings.move_lines:
            move.quantity_done = move.product_uom_qty
        pickings.button_validate()
        self.assertEqual(pickings.mapped("state"), ["done", "done"])

    def test_sync_picking(self):
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True

        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        sale = self._approve_po(purchase)

        self.assertTrue(purchase.picking_ids)
        self.assertTrue(sale.picking_ids)

        # validate the SO picking
        po_picking_id = purchase.picking_ids
        so_picking_id = sale.picking_ids

        so_picking_id.move_lines.quantity_done = 2

        self.assertNotEqual(po_picking_id, so_picking_id)
        self.assertNotEqual(
            po_picking_id.move_lines.quantity_done,
            so_picking_id.move_lines.quantity_done,
        )
        self.assertEqual(
            po_picking_id.move_lines.product_qty,
            so_picking_id.move_lines.product_qty,
        )

        so_picking_id.state = "done"
        wizard_data = so_picking_id.with_user(self.user_company_b).button_validate()
        wizard = (
            self.env["stock.backorder.confirmation"]
            .with_context(**wizard_data.get("context"))
            .create({})
        )
        wizard.process()

        # Quantities should have been synced
        self.assertNotEqual(po_picking_id, so_picking_id)
        self.assertEqual(
            po_picking_id.move_lines.quantity_done,
            so_picking_id.move_lines.quantity_done,
        )

        # A backorder should have been made for both
        self.assertTrue(len(sale.picking_ids) > 1)
        self.assertEqual(len(purchase.picking_ids), len(sale.picking_ids))

    def test_sync_picking_lot(self):
        """
        Test that the lot is synchronized on the moves
        by searching or creating a new lot in the company of destination
        """
        # lot 3 already exists in company_a
        serial_3_company_a = self._create_serial_and_quant(
            self.stockable_product_serial, "333", self.company_a
        )
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True

        purchase = self._create_purchase_order(
            self.partner_company_b, self.stockable_product_serial
        )
        sale = self._approve_po(purchase)

        # validate the SO picking
        po_picking_id = purchase.picking_ids
        so_picking_id = sale.picking_ids

        so_move = so_picking_id.move_lines
        so_move.move_line_ids = [
            (
                0,
                0,
                {
                    "location_id": so_move.location_id.id,
                    "location_dest_id": so_move.location_dest_id.id,
                    "product_id": self.stockable_product_serial.id,
                    "product_uom_id": self.stockable_product_serial.uom_id.id,
                    "qty_done": 1,
                    "lot_id": self.serial_1.id,
                    "picking_id": so_picking_id.id,
                },
            ),
            (
                0,
                0,
                {
                    "location_id": so_move.location_id.id,
                    "location_dest_id": so_move.location_dest_id.id,
                    "product_id": self.stockable_product_serial.id,
                    "product_uom_id": self.stockable_product_serial.uom_id.id,
                    "qty_done": 1,
                    "lot_id": self.serial_2.id,
                    "picking_id": so_picking_id.id,
                },
            ),
            (
                0,
                0,
                {
                    "location_id": so_move.location_id.id,
                    "location_dest_id": so_move.location_dest_id.id,
                    "product_id": self.stockable_product_serial.id,
                    "product_uom_id": self.stockable_product_serial.uom_id.id,
                    "qty_done": 1,
                    "lot_id": self.serial_3.id,
                    "picking_id": so_picking_id.id,
                },
            ),
        ]
        so_picking_id.button_validate()

        so_lots = so_move.mapped("move_line_ids.lot_id")
        po_lots = po_picking_id.mapped("move_lines.move_line_ids.lot_id")
        self.assertEqual(
            len(so_lots),
            len(po_lots),
            msg="There aren't the same number of lots on both moves",
        )
        self.assertNotEqual(
            so_lots, po_lots, msg="The lots of the moves should be different objects"
        )
        self.assertEqual(
            so_lots.mapped("name"),
            po_lots.mapped("name"),
            msg="The lots should have the same name in both moves",
        )
        self.assertIn(
            serial_3_company_a,
            po_lots,
            msg="Serial 333 already existed, a new one shouldn't have been created",
        )
    
    def test_update_open_sale_order(self):
        """
        When the purchase user request extra product, the sale order gets synched if
        it's open.
        """
        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        purchase.picking_type_id = self.warehouse_a.in_type_id
        sale = self._approve_po(purchase)
        sale.action_confirm()
        # Now we add an extra product to the PO and it will show up in the SO
        po_form = Form(purchase)
        with po_form.order_line.new() as line:
            line.product_id = self.consumable_product_2
            line.product_qty = 6
        po_form.save()
        # It's synched and the values match
        synched_order_line = sale.order_line.filtered(
            lambda x: x.product_id == self.consumable_product_2
        )
        self.assertTrue(
            bool(synched_order_line),
            "The line should have been created in the sale order",
        )
        self.assertEqual(
            synched_order_line.product_uom_qty,
            6,
            "The quantity should be equal to the one set in the purchase order",
        )
        # Also the moves match as well
        so_picking_id = sale.picking_ids
        synched_move = so_picking_id.move_lines.filtered(
            lambda x: x.product_id == self.consumable_product_2
        )
        self.assertTrue(
            bool(synched_move),
            "The move should have been created in the delivery order",
        )
        self.assertEqual(
            synched_move.product_uom_qty,
            6,
            "The quantity should be equal to the one set in the purchase order",
        )
        # The quantity is synched as well
        purchase_line = purchase.order_line.filtered(
            lambda x: x.product_id == self.consumable_product_2
        ).sudo()
        purchase_line.product_qty = 8
        self.assertEqual(
            synched_order_line.product_uom_qty,
            8,
            "The quantity should be equal to the one set in the purchase order",
        )
        self.assertEqual(
            synched_move.product_uom_qty,
            8,
            "The quantity should synched to the one set in the purchase order",
        )
        # Let's decrease the quantity
        purchase_line.product_qty = 3
        self.assertEqual(
            synched_order_line.product_uom_qty,
            3,
            "The quantity should decrease as it was in the purchase order",
        )
        self.assertEqual(
            synched_move.product_uom_qty,
            8,
            "The quantity should remain as it was as it can't be decreased from the SO",
        )
        # A warning activity is scheduled in the picking
        self.assertRegex(
            so_picking_id.activity_ids.note,
            re.compile(
                "3.0 Units of Consumable Product 2.+instead of 8.0 Units", re.DOTALL
            ),
        )
