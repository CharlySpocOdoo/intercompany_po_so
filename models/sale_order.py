# -*- coding: utf-8 -*-
import logging

from odoo import models, fields, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    intercompany_po_id = fields.Many2one(
        comodel_name='purchase.order',
        string='Orden de Compra Origen',
        copy=False,
        readonly=True,
        help='Orden de compra en la empresa hermana que originó esta cotización automáticamente.',
    )

    def action_confirm(self):
        """
        Sobreescribe la confirmación de la cotización de venta.
        Después de confirmar normalmente, detecta si el cliente es una empresa
        hermana y genera una orden de compra en su contexto (flujo simétrico
        al de purchase_order.button_confirm).
        """
        res = super().action_confirm()

        for order in self:
            if order.state == 'sale':
                try:
                    order.create_intercompany_purchase_order()
                except Exception:
                    _logger.exception(
                        'No se pudo crear la orden de compra intercompañía para la cotización de venta %s.',
                        order.name,
                    )

        return res

    def create_intercompany_purchase_order(self):
        """
        Lógica principal (espejo de _create_intercompany_sale_order):
        1. Evita duplicados si ya existe una PO generada.
        2. Verifica si el cliente está vinculado a una empresa hermana.
        3. Crea la PO en borrador (RFQ) en el contexto de la empresa hermana.
        4. Vincula ambos documentos entre sí.
        """
        self.ensure_one()

        # --- 1. Verificar si ya existe una PO intercompañía vinculada ---
        if self.intercompany_po_id:
            return

        # --- 2. Verificar si el cliente es una empresa hermana ---
        customer_partner = self.partner_id

        # El partner puede ser un contacto hijo, subimos al partner comercial
        commercial_partner = customer_partner.commercial_partner_id

        # Buscamos si ese partner está vinculado a alguna empresa de la BD
        dest_company = self.env['res.company'].sudo().search([
            ('partner_id', '=', commercial_partner.id)
        ], limit=1)

        if not dest_company:
            # No es empresa hermana, flujo normal
            return

        if dest_company.id == self.company_id.id:
            # Es la misma empresa, no aplica
            return

        # --- 3. Identificar el partner de la empresa vendedora (Empresa A) ---
        # en el contexto de la empresa compradora (Empresa B)
        src_company_partner = self.company_id.partner_id

        # Verificar que el partner de Empresa A exista y sea accesible en Empresa B
        vendor_partner = self.env['res.partner'].sudo().with_company(dest_company).browse(
            src_company_partner.id
        )

        if not vendor_partner.exists():
            raise UserError(_(
                'No se puede crear la orden de compra intercompañía. '
                'El partner de la empresa "%s" no es accesible desde la empresa "%s".'
            ) % (self.company_id.name, dest_company.name))

        # --- 4. Preparar y crear la Purchase Order en Empresa B ---
        PurchaseOrder = self.env['purchase.order'].sudo().with_company(dest_company)

        po_vals = self._prepare_intercompany_po_vals(dest_company, vendor_partner)
        purchase_order = PurchaseOrder.create(po_vals)

        # --- 5. Crear las líneas de la PO (todas juntas, en una sola llamada a create) ---
        pol_vals_list = []
        for sale_line in self.order_line:
            if sale_line.display_type:
                # Líneas de sección o nota, se copian tal cual
                pol_vals_list.append({
                    'order_id': purchase_order.id,
                    'display_type': sale_line.display_type,
                    'name': sale_line.name,
                })
            else:
                pol_vals_list.append(self._prepare_intercompany_pol_vals(sale_line, purchase_order))

        if pol_vals_list:
            self.env['purchase.order.line'].sudo().with_company(dest_company).create(pol_vals_list)

        # --- 6. Vincular ambos documentos entre sí (trazabilidad bidireccional) ---
        self.intercompany_po_id = purchase_order.id
        purchase_order.sudo().intercompany_so_id = self.id

        # --- 7. Registrar mensaje en el chatter de ambos documentos ---
        self.message_post(
            body=_('Se generó automáticamente la orden de compra intercompañía <b>%s</b> en la empresa <b>%s</b>.')
            % (purchase_order.name, dest_company.name)
        )
        purchase_order.message_post(
            body=_('Orden de compra generada automáticamente desde la cotización de venta <b>%s</b> de la empresa <b>%s</b>.')
            % (self.name, self.company_id.name)
        )

    def action_open_intercompany_po(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'res_id': self.intercompany_po_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _prepare_intercompany_po_vals(self, dest_company, vendor_partner):
        """
        Prepara el diccionario de valores para crear la Purchase Order en Empresa B.
        """
        self.ensure_one()
        return {
            'company_id': dest_company.id,
            'partner_id': vendor_partner.id,
            'origin': self.name,
            'state': 'draft',
        }

    def _prepare_intercompany_pol_vals(self, sale_line, purchase_order):
        """
        Prepara el diccionario de valores para cada línea de la Purchase Order.
        Toma producto, cantidad, precio y descripción de la línea de la SO.
        """
        return {
            'order_id': purchase_order.id,
            'product_id': sale_line.product_id.id,
            'name': sale_line.name,
            'product_qty': sale_line.product_uom_qty,
            'product_uom': sale_line.product_uom.id,
            'price_unit': sale_line.price_unit,
        }
