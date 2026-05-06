# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    intercompany_so_id = fields.Many2one(
        comodel_name='sale.order',
        string='Cotización Intercompañía',
        copy=False,
        readonly=True,
        help='Cotización de venta generada automáticamente en la empresa proveedora hermana.',
    )

    def button_confirm(self):
        """
        Sobreescribe la confirmación de la PO.
        Después de confirmar normalmente, detecta si el proveedor es una empresa
        hermana y genera una cotización de venta en su contexto.
        """
        res = super().button_confirm()

        for order in self:
            order._create_intercompany_sale_order()

        return res

    def _create_intercompany_sale_order(self):
        """
        Lógica principal:
        1. Verifica si el proveedor está vinculado a una empresa hermana.
        2. Evita duplicados si ya existe una SO generada.
        3. Crea la SO en borrador en el contexto de la empresa hermana.
        4. Vincula la SO creada al campo intercompany_so_id de esta PO.
        """
        self.ensure_one()

        # --- 1. Verificar si ya existe una SO intercompañía vinculada ---
        if self.intercompany_so_id:
            return

        # --- 2. Verificar si el proveedor es una empresa hermana ---
        vendor_partner = self.partner_id

        # El partner puede ser un contacto hijo, subimos al partner comercial
        commercial_partner = vendor_partner.commercial_partner_id

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

        # --- 3. Identificar el partner de la empresa compradora (Empresa A) ---
        # en el contexto de la empresa vendedora (Empresa B)
        src_company_partner = self.company_id.partner_id

        # Verificar que el partner de Empresa A exista y sea accesible en Empresa B
        customer_partner = self.env['res.partner'].sudo().with_company(dest_company).browse(
            src_company_partner.id
        )

        if not customer_partner.exists():
            raise UserError(_(
                'No se puede crear la cotización intercompañía. '
                'El partner de la empresa "%s" no es accesible desde la empresa "%s".'
            ) % (self.company_id.name, dest_company.name))

        # --- 4. Preparar y crear la Sale Order en Empresa B ---
        SaleOrder = self.env['sale.order'].sudo().with_company(dest_company)

        so_vals = self._prepare_intercompany_so_vals(dest_company, customer_partner)
        sale_order = SaleOrder.create(so_vals)

        # --- 5. Crear las líneas de la SO ---
        for po_line in self.order_line:
            if po_line.display_type:
                # Líneas de sección o nota, se copian tal cual
                self.env['sale.order.line'].sudo().with_company(dest_company).create({
                    'order_id': sale_order.id,
                    'display_type': po_line.display_type,
                    'name': po_line.name,
                })
            else:
                sol_vals = self._prepare_intercompany_sol_vals(po_line, sale_order)
                self.env['sale.order.line'].sudo().with_company(dest_company).create(sol_vals)

        # --- 6. Vincular ambos documentos entre sí (trazabilidad bidireccional) ---
        self.intercompany_so_id = sale_order.id
        sale_order.sudo().intercompany_po_id = self.id

        # --- 7. Registrar mensaje en el chatter de ambos documentos ---
        self.message_post(
            body=_('Se generó automáticamente la cotización intercompañía <b>%s</b> en la empresa <b>%s</b>.')
            % (sale_order.name, dest_company.name)
        )
        sale_order.message_post(
            body=_('Cotización generada automáticamente desde la orden de compra <b>%s</b> de la empresa <b>%s</b>.')
            % (self.name, self.company_id.name)
        )

    def _prepare_intercompany_so_vals(self, dest_company, customer_partner):
        """
        Prepara el diccionario de valores para crear la Sale Order en Empresa B.
        """
        self.ensure_one()
        return {
            'company_id': dest_company.id,
            'partner_id': customer_partner.id,
            # Referencia a la PO origen — visible en pestaña "Otra información"
            'client_order_ref': _('PO de %s: %s') % (self.company_id.name, self.name),
            'origin': self.name,
            'state': 'draft',
            'note': _(
                'Cotización generada automáticamente por la orden de compra %s '
                'de la empresa %s.'
            ) % (self.name, self.company_id.name),
        }

    def _prepare_intercompany_sol_vals(self, po_line, sale_order):
        """
        Prepara el diccionario de valores para cada línea de la Sale Order.
        Toma producto, cantidad, precio y descripción de la línea de PO.
        """
        return {
            'order_id': sale_order.id,
            'product_id': po_line.product_id.id,
            'name': po_line.name,
            'product_uom_qty': po_line.product_qty,
            'product_uom': po_line.product_uom.id,
            'price_unit': po_line.price_unit,
        }
