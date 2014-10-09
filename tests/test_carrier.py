# -*- coding: utf-8 -*-
"""
    test_carrier

    Test USPS Integration via Endicia.

    :copyright: (c) 2013-2014 by Openlabs Technologies & Consulting (P) Limited
    :license: GPLv3, see LICENSE for more details.
"""

from decimal import Decimal

from trytond.tests.test_tryton import DB_NAME, USER, CONTEXT
from trytond.transaction import Transaction
from tests.test_endicia import BaseTestCase


class CarrierTestCase(BaseTestCase):
    """
    Test Carrier model class.
    """

    def setup_defaults(self):
        """
        Method to setup defaults
        """
        super(CarrierTestCase, self).setup_defaults()

        self.carrier_product.code = "PROD123_US"
        self.carrier_product.save()

    def test_get_rates(self):
        """
        Test the get_rates method of Carrier.
        """
        with Transaction().start(DB_NAME, USER, context=CONTEXT) as trx:
            self.setup_defaults()

            trx.set_context(sale=None)
            self.assertEquals(self.carrier.get_rates(), [])

            trx.set_context(sale=self.sale)

            for tuple in self.carrier.get_rates():
                self.assertIn('PROD123_US', tuple[0])
                self.assertIsInstance(tuple[1], Decimal)
                self.assert_(tuple[2], self.currency)
                self.assertIn('endicia_mailclass', tuple[4])
                self.assertIn('carrier', tuple[4])

    def test_get_sale_price(self):
        """
        Tests the get_sale_price() method in Carrier model class.
        """
        with Transaction().start(DB_NAME, USER, context=CONTEXT) as trx:
            self.setup_defaults()
            shipment, = self.StockShipmentOut.search([])
            self.StockShipmentOut.write([shipment], {
                'code': '1234'
            })

            trx.set_context(sale=None, shipment=None)
            self.assertEquals(
                self.carrier.get_sale_price(),
                (Decimal('0'), self.currency.id)
            )

            trx.set_context(sale=self.sale, shipment=shipment)
            self.carrier.carrier_cost_method = 'product'
            self.carrier.save()
            self.assertEquals(
                self.carrier.get_sale_price(),
                super(self.Carrier, self.carrier).get_sale_price()
            )

            self.carrier.carrier_cost_method = 'endicia'
            self.carrier.save()

            trx.set_context(sale=None, shipment=shipment)
            self.assertEquals(
                self.carrier.get_sale_price(),
                (shipment.get_endicia_shipping_cost(), self.currency.id)
            )

            trx.set_context(sale=self.sale, shipment=None)
            self.assertEquals(
                self.carrier.get_sale_price(),
                (self.sale.get_endicia_shipping_cost(), self.currency.id)
            )
