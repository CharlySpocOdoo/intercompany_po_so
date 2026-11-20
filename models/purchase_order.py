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

    # 1.2.0: quién creó a quién. El vínculo intercompany_so_id es igual en los dos sentidos; este campo los distingue.
    intercompany_origen = fields.Selection(
        [('compra', 'La compra creó la venta'), ('venta', 'La venta creó la compra')],
        string='Origen intercompañía',
        copy=False,
        readonly=True,
        help='«compra»: esta compra creó la venta de la empresa hermana; los cambios de la compra se pasan a la '
             'venta o se bloquean. «venta»: esta compra nació de una venta de la hermana; no se toca la venta.',
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
                    'intercompany_purchase_line_id': po_line.id,      # 1.2.0: vínculo de líneas
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
        # 1.2.0: en la misma escritura, el sentido: esta compra es el origen de la venta.
        self.write({'intercompany_so_id': sale_order.id, 'intercompany_origen': 'compra'})
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
            'intercompany_purchase_line_id': po_line.id,          # 1.2.0: vínculo de líneas
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

    # ── 1.2.0 · Cambios de la compra después de crear la venta (Charly, 2026-11-19, «la A») ─────────────────────
    # Solo cuando ESTA compra es el origen de la venta (intercompany_origen = 'compra'). Con 'venta' no se hace nada.

    def _intercompany_origen_efectivo(self):
        """'compra', 'venta' o False. Si el campo está vacío (compras de antes de la 1.2.0), lo deduce y lo guarda:
        1) la venta tiene líneas con vínculo de línea → compra; 2) origin (cada flujo pone el nombre del documento de
        origen en el otro); 3) cuál se creó primero, con al menos un segundo de diferencia. Si nada decide, avisa en la
        conversación de la compra y no se toca nada."""
        self.ensure_one()
        if not self.intercompany_so_id:
            return False
        if self.intercompany_origen:
            return self.intercompany_origen
        venta = self.intercompany_so_id.sudo()
        origen = False
        if venta.order_line.filtered('intercompany_purchase_line_id'):
            origen = 'compra'
        elif venta.origin == self.name and self.origin != venta.name:
            origen = 'compra'
        elif self.origin == venta.name and venta.origin != self.name:
            origen = 'venta'
        elif self.create_date and venta.create_date and abs((self.create_date - venta.create_date).total_seconds()) >= 1:
            origen = 'compra' if self.create_date < venta.create_date else 'venta'
        if origen:
            self.sudo().with_context(intercompany_sincronizando=True).write({'intercompany_origen': origen})
        else:
            self.message_post(body=_(
                'No se pudo saber si esta compra creó la venta %s o nació de ella. No se actualizó ni se bloqueó '
                'nada en la venta. Revisa a mano.'
            ) % venta.name)
        return origen

    @staticmethod
    def _intercompany_venta_editable(venta):
        return venta.state in ('draft', 'sent')

    def _intercompany_emparejar(self, venta, excluir=None):
        """Ventas de antes de la 1.2.0, sin vínculo de líneas: empareja por producto y posición (la 1.ª línea de
        compra del producto X con la 1.ª de venta del producto X…). Solo si TODO empareja; si no, regresa False."""
        lineas_v = venta.order_line.filtered(lambda s: not s.display_type)
        if lineas_v.filtered('intercompany_purchase_line_id'):
            return True
        excluir = excluir or self.env['purchase.order.line']
        lineas_c = self.order_line.filtered(lambda l: not l.display_type and l.id not in excluir.ids)

        def por_producto(lineas):
            grupos = {}
            for linea in lineas.sorted(lambda x: (x.sequence, x.id)):
                grupos.setdefault(linea.product_id.id, []).append(linea)
            return grupos

        gc, gv = por_producto(lineas_c), por_producto(lineas_v)
        if set(gc) != set(gv) or any(len(gc[p]) != len(gv[p]) for p in gc):
            return False
        for producto, compras in gc.items():
            for linea_c, linea_v in zip(compras, gv[producto]):
                linea_v.with_context(intercompany_sincronizando=True).write(
                    {'intercompany_purchase_line_id': linea_c.id})
        return True

    def _intercompany_valores_actualizar(self, po_line, venta, empresa, con_producto):
        """Los valores para que la línea de venta quede como la de compra: cantidad, unidad, precio (regla 1.1.0) y
        descuento, siempre juntos para que la lista de precios de la venta no los recalcule."""
        vals = self._prepare_intercompany_sol_vals(po_line, venta, empresa)
        vals.pop('order_id', None)
        vals.pop('intercompany_purchase_line_id', None)
        if not con_producto:
            vals.pop('product_id', None)
            vals.pop('name', None)
        vals.setdefault('discount', 0.0)
        return vals

    def _intercompany_nota(self, venta, texto_compra, texto_venta, detalle):
        """Nota en las DOS conversaciones."""
        filas = Markup('').join(Markup('<li>%s</li>') % d for d in detalle)
        self.message_post(body=Markup('<p>%s</p><ul>%s</ul>') % (texto_compra, filas))
        venta.message_post(body=Markup('<p>%s</p><ul>%s</ul>') % (texto_venta, filas))

    def _intercompany_pasar_a_venta(self, lineas, antes=None):
        """Después de crear o cambiar líneas de esta compra (origen de la venta):
        - venta en borrador o enviada: la línea de venta vinculada queda como la de compra (se crea si falta);
          se revisan los totales y queda nota en las dos conversaciones;
        - venta confirmada o cancelada: se deja guardar y queda un aviso en las dos conversaciones con lo que cambió.
        Es idempotente: si la línea de venta ya está igual, no hace nada."""
        self.ensure_one()
        venta = self.intercompany_so_id.sudo()
        empresa = venta.company_id
        antes = antes or {}
        cambios = []
        for linea in lineas.filtered(lambda l: not l.display_type):
            despues = linea._intercompany_foto()
            previo = antes.get(linea.id)
            if previo is None or linea._intercompany_difiere(previo, despues):
                cambios.append((linea, previo, despues))
        if not cambios:
            return
        if not self._intercompany_venta_editable(venta):
            estado = _('cancelada') if venta.state == 'cancel' else _('confirmada')
            detalle = [linea._intercompany_describir(previo, despues) for linea, previo, despues in cambios]
            self._intercompany_nota(
                venta,
                _('La compra cambió, pero la venta %(venta)s de %(empresa)s ya está %(estado)s y NO se actualizó. '
                  'Ajústala en %(empresa)s.', venta=venta.name, empresa=empresa.name, estado=estado),
                _('La compra %(compra)s de %(empresa)s cambió, pero esta venta ya está %(estado)s y NO se actualizó. '
                  'Revisa si hay que ajustarla.', compra=self.name, empresa=self.company_id.name, estado=estado),
                detalle)
            return
        if not self._intercompany_emparejar(venta, excluir=lineas.filtered(lambda l: l.id not in antes)):
            self._intercompany_nota(
                venta,
                _('No se pudo relacionar la venta %s con esta compra, línea por línea. No se actualizó. Revisa a mano.')
                % venta.name,
                _('No se pudo relacionar esta venta con la compra %s, línea por línea. No se actualizó. Revisa a mano.')
                % self.name,
                [linea._intercompany_describir(previo, despues) for linea, previo, despues in cambios])
            return
        LineaVenta = self.env['sale.order.line'].sudo().with_company(empresa).with_context(
            intercompany_sincronizando=True)
        pares, detalle = [], []
        for linea, previo, despues in cambios:
            linea_v = venta.order_line.filtered(lambda s, l=linea: s.intercompany_purchase_line_id.id == l.id)
            if linea_v:
                con_producto = bool(previo) and previo['product_id'] != despues['product_id']
                vals = self._intercompany_valores_actualizar(linea, venta, empresa, con_producto)
                antes_v = [(v.product_uom_qty, v.price_unit, v.discount) for v in linea_v]
                linea_v.with_company(empresa).with_context(intercompany_sincronizando=True).write(vals)
                texto = linea._intercompany_describir(previo, despues) + _(
                    ' → venta: antes %(antes)s; ahora cantidad %(cant)s, precio %(precio)s, descuento %(desc)s %%',
                    antes='; '.join(_('cantidad %(c)s, precio %(p)s, descuento %(d)s %%', c=_n(c), p=_n(p), d=_n(d))
                                    for c, p, d in antes_v),
                    cant=_n(vals.get('product_uom_qty', 0.0)), precio=_n(vals.get('price_unit', 0.0)),
                    desc=_n(vals.get('discount', 0.0)))
            else:
                linea_v = LineaVenta.create(self._prepare_intercompany_sol_vals(linea, venta, empresa))
                texto = linea._intercompany_describir(None, despues) + _(' → se agregó a la venta.')
            pares += [(linea, v) for v in linea_v]
            detalle.append(texto)
        self._intercompany_nota(
            venta,
            _('Se actualizó la venta %(venta)s de %(empresa)s por cambios en esta compra:',
              venta=venta.name, empresa=empresa.name),
            _('Actualizada desde la compra %(compra)s de %(empresa)s:', compra=self.name, empresa=self.company_id.name),
            detalle)
        self._intercompany_revisar_totales(venta, pares, empresa)

    def _intercompany_lineas_quitadas(self, lineas):
        """Antes de borrar líneas de esta compra (origen de la venta): con la venta en borrador o enviada, se quitan
        sus líneas de venta; si no, aviso en las dos conversaciones."""
        self.ensure_one()
        venta = self.intercompany_so_id.sudo()
        empresa = venta.company_id
        de_producto = lineas.filtered(lambda l: not l.display_type)
        detalle = [_('%(producto)s: cantidad %(cant)s', producto=l.product_id.display_name or l.name,
                     cant=_n(l.product_qty)) for l in de_producto]
        if not self._intercompany_venta_editable(venta):
            if detalle:
                estado = _('cancelada') if venta.state == 'cancel' else _('confirmada')
                self._intercompany_nota(
                    venta,
                    _('Se quitaron líneas de la compra, pero la venta %(venta)s ya está %(estado)s y NO se actualizó. '
                      'Ajústala en %(empresa)s.', venta=venta.name, estado=estado, empresa=empresa.name),
                    _('Se quitaron líneas de la compra %(compra)s, pero esta venta ya está %(estado)s y NO se '
                      'actualizó. Revisa si hay que ajustarla.', compra=self.name, estado=estado),
                    detalle)
            return
        if not self._intercompany_emparejar(venta):
            if detalle:
                self._intercompany_nota(
                    venta,
                    _('No se pudo relacionar la venta %s con esta compra; no se quitaron líneas de la venta. Revisa a '
                      'mano.') % venta.name,
                    _('No se pudo relacionar esta venta con la compra %s; no se quitaron sus líneas. Revisa a mano.')
                    % self.name,
                    detalle)
            return
        lineas_v = venta.order_line.filtered(lambda s: s.intercompany_purchase_line_id.id in lineas.ids)
        if lineas_v:
            lineas_v.with_context(intercompany_sincronizando=True).unlink()
        if detalle:
            self._intercompany_nota(
                venta,
                _('Se quitaron de la venta %s las líneas que se quitaron de esta compra:') % venta.name,
                _('Se quitaron líneas porque se quitaron de la compra %s:') % self.name,
                detalle)


def _n(x):
    """12.0 → «12»; 115.9900 → «115.99». Para las notas."""
    return ('%.4f' % (x or 0.0)).rstrip('0').rstrip('.')
