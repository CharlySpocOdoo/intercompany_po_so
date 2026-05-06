# -*- coding: utf-8 -*-
{
    'name': 'Intercompany PO to SO',
    'version': '18.0.1.0.0',
    'summary': 'Genera automáticamente una cotización de venta en empresa hermana al confirmar una orden de compra intercompañía.',
    'description': """
        Al confirmar una Orden de Compra cuyo proveedor es una empresa hermana
        (registrada en la misma base de datos), se crea automáticamente una
        Cotización de Venta en borrador en dicha empresa hermana.
    """,
    'author': 'Desarrollo Personalizado',
    'category': 'Purchase',
    'depends': [
        'purchase',
        'sale_management',
    ],
    'data': [
        'views/purchase_order_views.xml',
        'views/sale_order_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
