# -*- coding: utf-8 -*-
from odoo import models, fields


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    intercompany_po_id = fields.Many2one(
        comodel_name='purchase.order',
        string='Orden de Compra Origen',
        copy=False,
        readonly=True,
        help='Orden de compra en la empresa hermana que originó esta cotización automáticamente.',
    )
