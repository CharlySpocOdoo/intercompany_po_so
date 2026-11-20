# -*- coding: utf-8 -*-
from odoo import _, api, models
from odoo.exceptions import UserError
from odoo.tools import float_compare

# 1.2.0 (Charly, 2026-11-19, «la A»), solo en compras que son ORIGEN de una venta en la hermana:
# - precio, descuento e impuestos: con la venta en borrador o enviada se pasan a la venta; con la venta confirmada o
#   cancelada NO se dejan cambiar (se revisa ANTES de escribir);
# - cantidad, unidad, producto, y agregar o quitar líneas: con la venta en borrador o enviada se pasan a la venta;
#   con la venta confirmada o cancelada se deja guardar y queda un aviso en las dos conversaciones.
# El gancho NO se salta el contexto rdl_iva_aplicando de rdl_compra_iva: así ve los cambios del selector.
CTX = 'intercompany_sincronizando'
CAMPOS_DINERO = frozenset({'taxes_id', 'price_unit', 'discount'})
CAMPOS_CANTIDAD = frozenset({'product_qty', 'product_uom', 'product_id'})
CLAVES_FOTO = ('product_id', 'cantidad', 'unidad', 'precio', 'descuento', 'impuestos', 'total')


def ids_tras_comandos(actuales, comandos):
    """Los ids de un many2many después de aplicar los comandos de una escritura; None si no se puede saber."""
    ids = set(actuales)
    if not isinstance(comandos, (list, tuple)):
        return None
    for c in comandos:
        if isinstance(c, int):                      # lista simple de ids: es el conjunto nuevo
            return set(i for i in comandos if isinstance(i, int))
        if not isinstance(c, (list, tuple)) or not c:
            return None
        op = c[0]
        if op == 6:
            ids = set(c[2] or [])
        elif op == 5:
            ids = set()
        elif op == 4:
            ids.add(c[1])
        elif op in (2, 3):
            ids.discard(c[1])
        else:                                       # crear o actualizar un impuesto desde aquí: no se puede saber
            return None
    return ids


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    # ── Fotos para comparar antes y después ──────────────────────────────────

    def _intercompany_foto(self):
        self.ensure_one()
        return {
            'producto': self.product_id.display_name or self.name or '',
            'product_id': self.product_id.id,
            'cantidad': self.product_qty,
            'unidad': self.product_uom.id,
            'precio': self.price_unit,
            'descuento': self.order_id._intercompany_descuento(self),
            'impuestos': tuple(sorted(self.taxes_id.ids)),
            'impuestos_nombre': ', '.join(self.taxes_id.mapped('name')) or _('sin impuesto'),
            'total': self.price_total,
        }

    @staticmethod
    def _intercompany_difiere(previo, despues):
        for clave in CLAVES_FOTO:
            a, b = previo.get(clave), despues.get(clave)
            if isinstance(a, float) or isinstance(b, float):
                if float_compare(a or 0.0, b or 0.0, precision_digits=6) != 0:
                    return True
            elif a != b:
                return True
        return False

    def _intercompany_describir(self, previo, despues):
        """«Producto: cantidad 10 → 8; precio 100 → 116; impuestos A → B». Sin «previo», es una línea nueva."""
        from .purchase_order import _n
        if not previo:
            return _('%(producto)s (nueva): cantidad %(cant)s, precio %(precio)s, descuento %(desc)s %%, impuestos '
                     '%(imp)s', producto=despues['producto'], cant=_n(despues['cantidad']),
                     precio=_n(despues['precio']), desc=_n(despues['descuento']), imp=despues['impuestos_nombre'])
        partes = []
        if previo['producto'] != despues['producto']:
            partes.append(_('producto %(a)s → %(b)s', a=previo['producto'], b=despues['producto']))
        for clave, etiqueta in (('cantidad', _('cantidad')), ('precio', _('precio')), ('descuento', _('descuento %'))):
            if float_compare(previo[clave] or 0.0, despues[clave] or 0.0, precision_digits=6) != 0:
                partes.append('%s %s → %s' % (etiqueta, _n(previo[clave]), _n(despues[clave])))
        if previo['impuestos'] != despues['impuestos']:
            partes.append(_('impuestos %(a)s → %(b)s', a=previo['impuestos_nombre'], b=despues['impuestos_nombre']))
        if float_compare(previo['total'] or 0.0, despues['total'] or 0.0, precision_digits=6) != 0:
            partes.append(_('total %(a)s → %(b)s', a=_n(previo['total']), b=_n(despues['total'])))
        return '%s: %s' % (despues['producto'], '; '.join(partes) or _('sin cambios de importe'))

    # ── Qué compras cuentan ──────────────────────────────────────────────────

    def _intercompany_por_compra(self):
        """{compra: líneas}, solo de compras que son ORIGEN de una venta en la hermana."""
        grupos = {}
        for linea in self:
            orden = linea.order_id
            if not orden.intercompany_so_id:
                continue
            if orden not in grupos:
                if orden._intercompany_origen_efectivo() != 'compra':
                    grupos[orden] = None
                    continue
                grupos[orden] = self.browse()
            if grupos[orden] is not None:
                grupos[orden] |= linea
        return {o: l for o, l in grupos.items() if l}

    def _intercompany_cambia_dinero(self, vals):
        """Si la escritura cambia DE VERDAD el precio, el descuento o los impuestos de esta línea."""
        self.ensure_one()
        if 'price_unit' in vals and float_compare(vals['price_unit'] or 0.0, self.price_unit,
                                                  precision_digits=6) != 0:
            return True
        if 'discount' in vals and float_compare(vals['discount'] or 0.0, self.order_id._intercompany_descuento(self),
                                                precision_digits=6) != 0:
            return True
        if 'taxes_id' in vals:
            nuevos = ids_tras_comandos(self.taxes_id.ids, vals['taxes_id'])
            if nuevos is None or nuevos != set(self.taxes_id.ids):
                return True
        return False

    def _intercompany_tiene_linea_venta(self, venta):
        """Si esta línea de compra ya tiene su línea en la venta. Con vínculo, por el vínculo. Ventas viejas sin
        ningún vínculo: si la venta tiene una línea del mismo producto."""
        self.ensure_one()
        # Una línea que se acaba de crear en esta misma operación (por ejemplo, rdl_compra_iva poniéndole su impuesto
        # justo después de crearla) todavía no está en la venta: nunca se bloquea.
        try:
            if self.create_date and self.create_date == self.env.cr.now():
                return False
        except AttributeError:
            pass
        lineas_v = venta.order_line.filtered(lambda s: not s.display_type)
        if lineas_v.filtered('intercompany_purchase_line_id'):
            return bool(lineas_v.filtered(lambda s: s.intercompany_purchase_line_id.id == self.id))
        return bool(lineas_v.filtered(lambda s: s.product_id == self.product_id))

    # ── Los ganchos ──────────────────────────────────────────────────────────

    def write(self, vals):
        if self.env.context.get(CTX) or not (set(vals) & (CAMPOS_DINERO | CAMPOS_CANTIDAD)):
            return super().write(vals)
        grupos = self._intercompany_por_compra()
        if not grupos:
            return super().write(vals)
        # 1. ANTES de escribir: precio, descuento o impuesto que cambian de verdad, en una línea que ya está en una
        #    venta confirmada o cancelada → no se deja. Así no se escribe nada y la compra queda como estaba.
        for orden, lineas in grupos.items():
            venta = orden.intercompany_so_id.sudo()
            if orden._intercompany_venta_editable(venta):
                continue
            for linea in lineas.filtered(lambda l: not l.display_type):
                if linea._intercompany_cambia_dinero(vals) and linea._intercompany_tiene_linea_venta(venta):
                    estado = _('cancelada') if venta.state == 'cancel' else _('confirmada')
                    raise UserError(_(
                        'No se puede cambiar el precio, el descuento o el impuesto de «%(producto)s» en la compra '
                        '%(compra)s: la venta %(venta)s de %(empresa)s, que se creó desde esta compra, ya está '
                        '%(estado)s y no se puede actualizar. Pide en %(empresa)s que regresen la venta a cotización, o '
                        'haz el cambio antes de que la confirmen.',
                        producto=linea.product_id.display_name or linea.name, compra=orden.name, venta=venta.name,
                        empresa=venta.company_id.name, estado=estado))
        antes = {l.id: l._intercompany_foto() for lineas in grupos.values() for l in lineas if not l.display_type}
        res = super().write(vals)
        # 2. DESPUÉS: pasar a la venta (borrador o enviada) o avisar (confirmada o cancelada).
        for orden, lineas in grupos.items():
            orden._intercompany_pasar_a_venta(lineas, antes)
        return res

    @api.model_create_multi
    def create(self, vals_list):
        lineas = super().create(vals_list)
        if not self.env.context.get(CTX):
            for orden, nuevas in lineas._intercompany_por_compra().items():
                orden._intercompany_pasar_a_venta(nuevas)
        return lineas

    def unlink(self):
        if not self.env.context.get(CTX):
            for orden, lineas in self._intercompany_por_compra().items():
                orden._intercompany_lineas_quitadas(lineas)
        return super().unlink()
