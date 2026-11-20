# -*- coding: utf-8 -*-
from odoo import fields, models


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    # 1.2.0: de qué línea de compra salió esta línea de venta (flujo compra → venta). Con esto, un cambio en la
    # línea de compra sabe qué línea de venta actualizar.
    intercompany_purchase_line_id = fields.Many2one(
        comodel_name='purchase.order.line',
        string='Línea de compra intercompañía',
        copy=False,
        readonly=True,
        index=True,
        ondelete='set null',
        help='La línea de la compra de la empresa hermana de la que salió esta línea de venta.',
    )
