# -*- coding: utf-8 -*-
import logging

from markupsafe import Markup

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools import float_round, format_amount

_logger = logging.getLogger(__name__)


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    intercompany_so_id = fields.Many2one(
        comodel_name='sale.order',
        string='Cotización Intercompañía',
        copy=False,
        readonly=True,
        help='Cotización de venta generada automáticamente en la empresa proveedora hermana.',
    )

    intercompany_so_button_label = fields.Char(
        compute='_compute_intercompany_so_button_label',
        store=False,
    )

    @api.depends('intercompany_so_id', 'intercompany_so_id.company_id')
    def _compute_intercompany_so_button_label(self):
        for record in self:
            if record.intercompany_so_id:
                record.intercompany_so_button_label = 'Venta Origen en ' + record.intercompany_so_id.company_id.name
            else:
                record.intercompany_so_button_label = ''

    def button_confirm(self):
        """
        Sobreescribe la confirmación de la PO.
        Después de confirmar normalmente, detecta si el proveedor es una empresa
        hermana y genera una cotización de venta en su contexto.
        """
        res = super().button_confirm()

        for order in self:
            if order.state == 'purchase':
                try:
                    order._create_intercompany_sale_order()
                except Exception:
                    _logger.exception(
                        'No se pudo crear la cotización intercompañía para la orden de compra %s.',
                        order.name,
                    )

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

        # --- 5. Crear las líneas de la SO (todas juntas, en una sola llamada a create) ---
        sol_vals_list = []
        po_lines_en_orden = []          # 1.1.0: la línea de compra de cada línea de venta, para revisar los totales
        for po_line in self.order_line:
            if po_line.display_type:
                # Líneas de sección o nota, se copian tal cual
                sol_vals_list.append({
                    'order_id': sale_order.id,
                    'display_type': po_line.display_type,
                    'name': po_line.name,
                })
                po_lines_en_orden.append(None)
            else:
                sol_vals_list.append(self._prepare_intercompany_sol_vals(po_line, sale_order, dest_company))
                po_lines_en_orden.append(po_line)

        if sol_vals_list:
            so_lines = self.env['sale.order.line'].sudo().with_company(dest_company).create(sol_vals_list)
            pares = [(po_l, so_l) for po_l, so_l in zip(po_lines_en_orden, so_lines) if po_l]
            self._intercompany_revisar_totales(sale_order, pares, dest_company)

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

    def action_open_intercompany_so(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': self.intercompany_so_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _prepare_intercompany_so_vals(self, dest_company, customer_partner):
        """
        Prepara el diccionario de valores para crear la Sale Order en Empresa B.
        """
        self.ensure_one()
        return {
            'company_id': dest_company.id,
            'partner_id': customer_partner.id,
            'origin': self.name,
            'state': 'draft',
            'note': _(
                'Cotización generada automáticamente por la orden de compra %s '
                'de la empresa %s.'
            ) % (self.name, self.company_id.name),
        }

    def _intercompany_precio_venta(self, po_line):
        """
        1.1.0 (Charly, 2026-11-19, «la B»): la venta de la hermana debe quedar con el MISMO TOTAL que la línea de
        compra. Las ventas siempre van con IVA incluido.
        - Si los impuestos de la línea de compra NO son de precio incluido (por ejemplo «16% No Incluido»), el precio
          de venta es el total con impuestos de la línea (price_total, que Odoo calcula con compute_all de esos
          impuestos y ya trae el descuento) entre la cantidad y entre (1 − descuento). Así, con el mismo descuento y
          el IVA incluido de la venta, el total de la venta es el de la compra.
        - Si son de precio incluido, o la línea no tiene impuestos, el precio se queda igual que antes (price_unit).
        Decide con los impuestos reales de la línea: no depende de rdl_compra_iva.
        Devuelve el precio en la moneda de la compra, sin redondear.
        """
        impuestos = po_line.taxes_id
        cantidad = po_line.product_qty
        factor = 1.0 - (self._intercompany_descuento(po_line) / 100.0)
        if not impuestos or all(impuestos.mapped('price_include')) or not cantidad or factor <= 0:
            return po_line.price_unit
        return po_line.price_total / (cantidad * factor)

    @staticmethod
    def _intercompany_descuento(po_line):
        """El descuento de la línea de compra (0 si el campo no existe)."""
        return (po_line.discount or 0.0) if 'discount' in po_line._fields else 0.0

    def _intercompany_a_moneda_destino(self, importe, dest_company):
        """Convierte un importe de la moneda de la compra a la de la empresa destino, SIN redondear."""
        src_currency = self.currency_id
        dest_currency = dest_company.currency_id
        if src_currency and dest_currency and src_currency != dest_currency:
            return src_currency._convert(
                importe, dest_currency, dest_company, self.date_order or fields.Date.context_today(self), round=False
            )
        return importe

    def _prepare_intercompany_sol_vals(self, po_line, sale_order, dest_company):
        """
        Prepara el diccionario de valores para cada línea de la Sale Order.
        Toma producto, cantidad, precio, descuento y descripción de la línea de PO.
        El price_unit se convierte de la moneda de la compañía origen a la
        moneda de la compañía destino usando el mecanismo estándar de Odoo.
        1.1.0: el precio sale de _intercompany_precio_venta (mismo total que la compra, «la B»), se redondea a los
        decimales de «Product Price», y el descuento de la compra se copia a la venta.
        """
        precio = self._intercompany_precio_venta(po_line)
        price_unit = self._intercompany_a_moneda_destino(precio, dest_company)
        decimales = self.env['decimal.precision'].precision_get('Product Price')
        price_unit = float_round(price_unit, precision_digits=decimales)

        vals = {
            'order_id': sale_order.id,
            'product_id': po_line.product_id.id,
            'name': po_line.name,
            'product_uom_qty': po_line.product_qty,
            'product_uom': po_line.product_uom.id,
            'price_unit': price_unit,
        }
        descuento = self._intercompany_descuento(po_line)
        if descuento:
            vals['discount'] = descuento
        return vals

    def _intercompany_revisar_totales(self, sale_order, pares, dest_company):
        """
        1.1.0: compara el total de cada línea de venta con el de su línea de compra. Si alguno no cuadra (por los
        decimales del precio, o porque el impuesto de venta del producto en la hermana no es de precio incluido),
        lo avisa en la conversación de la venta, con la diferencia. No ajusta nada y nunca falla en silencio: si la
        revisión misma fallara, también lo dice en la conversación.
        """
        try:
            moneda = sale_order.currency_id or dest_company.currency_id
            diferencias = []
            for po_line, so_line in pares:
                esperado = moneda.round(self._intercompany_a_moneda_destino(po_line.price_total, dest_company))
                obtenido = so_line.price_total
                if moneda.compare_amounts(esperado, obtenido) != 0:
                    diferencias.append((so_line.product_id.display_name or so_line.name, esperado, obtenido))
            if not diferencias:
                return
            filas = Markup('').join(
                Markup('<li>%s: compra %s, venta %s (diferencia %s)</li>') % (
                    nombre, format_amount(self.env, esperado, moneda), format_amount(self.env, obtenido, moneda),
                    format_amount(self.env, obtenido - esperado, moneda))
                for nombre, esperado, obtenido in diferencias
            )
            total = sum(o - e for _n, e, o in diferencias)
            sale_order.sudo().message_post(body=Markup(
                '<p><b>Revisión contra la compra %s:</b> %s línea(s) no dan exactamente el mismo total que la compra. '
                'Diferencia total: %s. Revisa el precio o el impuesto de esas líneas antes de confirmar.</p><ul>%s</ul>'
            ) % (self.name, len(diferencias), format_amount(self.env, total, moneda), filas))
            _logger.warning('Intercompañía: la cotización %s no cuadra con la compra %s en %s línea(s).',
                            sale_order.name, self.name, len(diferencias))
        except Exception:
            _logger.exception('No se pudieron revisar los totales de la cotización %s contra la compra %s.',
                              sale_order.name, self.name)
            sale_order.sudo().message_post(body=_(
                'No se pudieron revisar los totales contra la compra %s. Revisa a mano que los totales coincidan.'
            ) % self.name)
