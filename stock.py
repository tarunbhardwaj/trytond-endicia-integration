# -*- encoding: utf-8 -*-
# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.

'''
Inherit stock for endicia API
'''
from decimal import Decimal
import base64
import math

from endicia import ShippingLabelAPI, LabelRequest, RefundRequestAPI, \
    SCANFormAPI, BuyingPostageAPI, Element, CalculatingPostageAPI
from endicia.tools import objectify_response, get_images
from endicia.exceptions import RequestError

from trytond.model import ModelView, fields
from trytond.wizard import Wizard, StateView, Button
from trytond.transaction import Transaction
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval
from trytond.rpc import RPC

from .sale import ENDICIA_PACKAGE_TYPES


__metaclass__ = PoolMeta
__all__ = [
    'ShipmentOut', 'GenerateEndiciaLabelMessage', 'GenerateEndiciaLabel',
    'EndiciaRefundRequestWizardView', 'EndiciaRefundRequestWizard',
    'SCANFormWizardView', 'SCANFormWizard', 'BuyPostageWizardView',
    'BuyPostageWizard', 'StockMove',
]


class ShipmentOut:
    "Shipment Out"
    __name__ = 'stock.shipment.out'

    endicia_mailclass = fields.Many2One(
        'endicia.mailclass', 'MailClass', states={
            'readonly': ~Eval('state').in_(['packed', 'done']),
        }, depends=['state']
    )
    endicia_label_subtype = fields.Selection([
        ('None', 'None'),
        ('Integrated', 'Integrated')
    ], 'Label Subtype', states={
        'readonly': ~Eval('state').in_(['packed', 'done']),
    }, depends=['state'])
    endicia_integrated_form_type = fields.Selection([
        ('Form2976', 'Form2976(Same as CN22)'),
        ('Form2976A', 'Form2976(Same as CP72)'),
    ], 'Integrated Form Type', states={
        'readonly': ~Eval('state').in_(['packed', 'done']),
    }, depends=['state'])
    endicia_include_postage = fields.Boolean('Include Postage ?', states={
        'readonly': ~Eval('state').in_(['packed', 'done']),
    }, depends=['state'])
    endicia_package_type = fields.Selection(
        ENDICIA_PACKAGE_TYPES, 'Package Content Type', states={
            'readonly': ~Eval('state').in_(['packed', 'done']),
        }, depends=['state']
    )
    is_endicia_shipping = fields.Boolean(
        'Is Endicia Shipping', depends=['carrier']
    )
    tracking_number = fields.Char('Tracking Number', states={
        'readonly': ~Eval('state').in_(['packed', 'done']),
    })
    endicia_refunded = fields.Boolean('Refunded ?', readonly=True)

    @staticmethod
    def default_endicia_mailclass():
        Config = Pool().get('sale.configuration')
        config = Config(1)
        return config.endicia_mailclass and config.endicia_mailclass.id or None

    @staticmethod
    def default_endicia_label_subtype():
        Config = Pool().get('sale.configuration')
        config = Config(1)
        return config.endicia_label_subtype

    @staticmethod
    def default_endicia_integrated_form_type():
        Config = Pool().get('sale.configuration')
        config = Config(1)
        return config.endicia_integrated_form_type

    @staticmethod
    def default_endicia_include_postage():
        Config = Pool().get('sale.configuration')
        config = Config(1)
        return config.endicia_include_postage

    @staticmethod
    def default_endicia_package_type():
        Config = Pool().get('sale.configuration')
        config = Config(1)
        return config.endicia_package_type

    @classmethod
    def __setup__(cls):
        super(ShipmentOut, cls).__setup__()
        # There can be cases when people might want to use a different
        # shipment carrier after the shipment is marked as done
        cls.carrier.states = {
            'readonly': ~Eval('state').in_(['packed', 'done']),
        }
        cls._error_messages.update({
            'warehouse_address_required': 'Warehouse address is required.',
            'mailclass_missing':
                'Select a mailclass to ship using Endicia [USPS].',
            'error_label': 'Error in generating label "%s"',
            'tracking_number_already_present':
                'Tracking Number is already present for this shipment.',
            'invalid_state': 'Labels can only be generated when the '
                'shipment is in Packed or Done states only',
            'wrong_carrier': 'Carrier for selected shipment is not Endicia',
        })
        cls.__rpc__.update({
            'make_endicia_labels': RPC(readonly=False, instantiate=0),
            'get_endicia_shipping_cost': RPC(readonly=False, instantiate=0),
        })

    def on_change_carrier(self):
        res = super(ShipmentOut, self).on_change_carrier()

        res['is_endicia_shipping'] = self.carrier and \
            self.carrier.carrier_cost_method == 'endicia'

        return res

    def _get_carrier_context(self):
        "Pass shipment in the context"
        context = super(ShipmentOut, self)._get_carrier_context()

        if not self.carrier.carrier_cost_method == 'endicia':
            return context

        context = context.copy()
        context['shipment'] = self.id
        return context

    def _update_endicia_item_details(self, request):
        '''
        Adding customs items/info and form descriptions to the request

        :param request: Shipping Label API request instance
        '''
        User = Pool().get('res.user')

        user = User(Transaction().user)
        customsitems = []
        value = 0

        for move in self.outgoing_moves:
            # customs_details = (
                # move.product.name, float(move.product.list_price)
            # )
            new_item = [
                Element('Description', move.product.name[0:50]),
                Element('Quantity', int(math.ceil(move.quantity))),
                Element('Weight', int(move.get_weight_for_endicia())),
                Element('Value', float(move.product.list_price)),
            ]
            customsitems.append(Element('CustomsItem', new_item))
            value += float(move.product.list_price) * move.quantity

        request.add_data({
            'customsinfo': [
                Element('CustomsItems', customsitems),
                Element('ContentsType', self.endicia_package_type)
            ]
        })
        description = ','.join([
            move.product.name for move in self.outgoing_moves
        ])
        total_value = sum(map(
            lambda move: float(move.product.cost_price) * move.quantity,
            self.outgoing_moves
        ))
        request.add_data({
            'ContentsType': self.endicia_package_type,
            'Value': total_value,
            'Description': description[:50],
            'CustomsCertify': 'TRUE',   # TODO: Should this be part of config ?
            'CustomsSigner': user.name,
        })

    def make_endicia_labels(self):
        """
        Make labels for the given shipment

        :return: Tracking number as string
        """
        Attachment = Pool().get('ir.attachment')
        EndiciaConfiguration = Pool().get('endicia.configuration')

        if self.state not in ('packed', 'done'):
            self.raise_user_error('invalid_state')

        if not (
            self.carrier and
            self.carrier.carrier_cost_method == 'endicia'
        ):
            self.raise_user_error('wrong_carrier')

        if self.tracking_number:
            self.raise_user_error('tracking_number_already_present')

        endicia_credentials = EndiciaConfiguration(1).get_endicia_credentials()

        if not self.endicia_mailclass:
            self.raise_user_error('mailclass_missing')

        mailclass = self.endicia_mailclass.value
        label_request = LabelRequest(
            Test=endicia_credentials.is_test and 'YES' or 'NO',
            LabelType=(
                'International' in mailclass
            ) and 'International' or 'Default',
            # TODO: Probably the following have to be configurable
            ImageFormat="PNG",
            LabelSize="6x4",
            ImageResolution="203",
            ImageRotation="Rotate270",
        )

        move_weights = map(
            lambda move: move.get_weight_for_endicia(), self.outgoing_moves
        )
        shipping_label_request = ShippingLabelAPI(
            label_request=label_request,
            weight_oz=sum(move_weights),
            partner_customer_id=self.delivery_address.id,
            partner_transaction_id=self.id,
            mail_class=mailclass,
            accountid=endicia_credentials.account_id,
            requesterid=endicia_credentials.requester_id,
            passphrase=endicia_credentials.passphrase,
            test=endicia_credentials.is_test,
        )

        # From address is the warehouse location. So it must be filled.
        if not self.warehouse.address:
            self.raise_user_error('warehouse_address_required')

        shipping_label_request.add_data(
            self.warehouse.address.address_to_endicia_from_address().data
        )
        shipping_label_request.add_data(
            self.delivery_address.address_to_endicia_to_address().data
        )
        shipping_label_request.add_data({
            'LabelSubtype': self.endicia_label_subtype,
            'IncludePostage':
                self.endicia_include_postage and 'TRUE' or 'FALSE',
        })

        if self.endicia_label_subtype != 'None':
            # Integrated form type needs to be sent for international shipments
            shipping_label_request.add_data({
                'IntegratedFormType': self.endicia_integrated_form_type,
            })

        self._update_endicia_item_details(shipping_label_request)

        try:
            response = shipping_label_request.send_request()
        except RequestError, error:
            self.raise_user_error('error_label', error_args=(error,))
        else:
            result = objectify_response(response)

            tracking_number = result.TrackingNumber.pyval
            self.__class__.write([self], {
                'tracking_number': unicode(result.TrackingNumber.pyval),
                'cost': Decimal(str(result.FinalPostage.pyval)),
            })

            # Save images as attachments
            images = get_images(result)
            for (id, label) in images:
                Attachment.create([{
                    'name': "%s_%s_USPS-Endicia.png" % (tracking_number, id),
                    'data': buffer(base64.decodestring(label)),
                    'resource': '%s,%s' % (self.__name__, self.id)
                }])

            return tracking_number

    def get_endicia_shipping_cost(self):
        """Returns the calculated shipping cost as sent by endicia

        :returns: The shipping cost in USD
        """
        EndiciaConfiguration = Pool().get('endicia.configuration')
        endicia_credentials = EndiciaConfiguration(1).get_endicia_credentials()

        if not self.endicia_mailclass:
            self.raise_user_error('mailclass_missing')

        calculate_postage_request = CalculatingPostageAPI(
            mailclass=self.endicia_mailclass.value,
            weightoz=sum(map(
                lambda move: move.get_weight_for_endicia(), self.outgoing_moves
            )),
            from_postal_code=self.warehouse.address.zip[:5],
            to_postal_code=self.delivery_address.zip[:5],
            to_country_code=self.delivery_address.country.code,
            accountid=endicia_credentials.account_id,
            requesterid=endicia_credentials.requester_id,
            passphrase=endicia_credentials.passphrase,
            test=endicia_credentials.is_test,
        )

        response = calculate_postage_request.send_request()

        return Decimal(
            objectify_response(response).PostagePrice.get('TotalAmount')
        )


class GenerateEndiciaLabelMessage(ModelView):
    'Generate Endicia Labels Message'
    __name__ = 'generate.endicia.label.message'

    tracking_number = fields.Char("Tracking number", readonly=True)


class GenerateEndiciaLabel(Wizard):
    'Generate Endicia Labels'
    __name__ = 'generate.endicia.label'

    start = StateView(
        'generate.endicia.label.message',
        'endicia_integration.generate_endicia_label_message_view_form',
        [
            Button('Ok', 'end', 'tryton-ok'),
        ]
    )

    def default_start(self, data):
        Shipment = Pool().get('stock.shipment.out')

        try:
            shipment, = Shipment.browse(Transaction().context['active_ids'])
        except ValueError:
            self.raise_user_error(
                'This wizard can be called for only one shipment at a time'
            )

        tracking_number = shipment.make_endicia_labels()

        return {'tracking_number': str(tracking_number)}


class EndiciaRefundRequestWizardView(ModelView):
    """Endicia Refund Wizard View
    """
    __name__ = 'endicia.refund.wizard.view'

    refund_status = fields.Text('Refund Status', readonly=True,)
    refund_approved = fields.Boolean('Refund Approved ?', readonly=True,)


class EndiciaRefundRequestWizard(Wizard):
    """A wizard to cancel the current shipment and refund the cost
    """
    __name__ = 'endicia.refund.wizard'

    start = StateView(
        'endicia.refund.wizard.view',
        'endicia_integration.endicia_refund_wizard_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Request Refund', 'request_refund', 'tryton-ok'),
        ]
    )
    request_refund = StateView(
        'endicia.refund.wizard.view',
        'endicia_integration.endicia_refund_wizard_view_form', [
            Button('OK', 'end', 'tryton-ok'),
        ]
    )

    @classmethod
    def __setup__(self):
        super(EndiciaRefundRequestWizard, self).__setup__()
        self._error_messages.update({
            'wrong_carrier': 'Carrier for selected shipment is not Endicia'
        })

    def default_request_refund(self, data):
        """Requests the refund for the current shipment record
        and returns the response.
        """
        Shipment = Pool().get('stock.shipment.out')
        EndiciaConfiguration = Pool().get('endicia.configuration')

        # Getting the api credentials to be used in refund request generation
        # endicia credentials are in the format :
        # (account_id, requester_id, passphrase, is_test)
        endicia_credentials = EndiciaConfiguration(1).get_endicia_credentials()

        shipments = Shipment.browse(Transaction().context['active_ids'])

        # PICNumber is the argument name expected by endicia in API,
        # so its better to use the same name here for better understanding
        pic_numbers = []
        for shipment in shipments:
            if not (
                shipment.carrier and
                shipment.carrier.carrier_cost_method == 'endicia'
            ):
                self.raise_user_error('wrong_carrier')

            pic_numbers.append(shipment.tracking_number)

        test = endicia_credentials.is_test and 'Y' or 'N'

        refund_request = RefundRequestAPI(
            pic_numbers=pic_numbers,
            accountid=endicia_credentials.account_id,
            requesterid=endicia_credentials.requester_id,
            passphrase=endicia_credentials.passphrase,
            test=test,
        )
        response = refund_request.send_request()
        result = objectify_response(response)
        if str(result.RefundList.PICNumber.IsApproved) == 'YES':
            refund_approved = True
            # If refund is approved, then set the state of record
            # as cancel/refund
            shipment.__class__.write(
                [shipment], {'endicia_refunded': True}
            )
        else:
            refund_approved = False
        default = {
            'refund_status': unicode(result.RefundList.PICNumber.ErrorMsg),
            'refund_approved': refund_approved
        }
        return default


class SCANFormWizardView(ModelView):
    """Shipment SCAN Form Wizard View
    """
    __name__ = 'endicia.scanform.wizard.view'

    response = fields.Text('Response', readonly=True,)


class SCANFormWizard(Wizard):
    """A wizard to generate the SCAN Form for the current shipment record
    """
    __name__ = 'endicia.scanform.wizard'

    start = StateView(
        'endicia.scanform.wizard.view',
        'endicia_integration.endicia_scanform_wizard_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Make SCAN Form', 'make_scanform', 'tryton-ok'),
        ]
    )
    make_scanform = StateView(
        'endicia.scanform.wizard.view',
        'endicia_integration.endicia_scanform_wizard_view_form', [
            Button('OK', 'end', 'tryton-ok'),
        ]
    )

    @classmethod
    def __setup__(self):
        super(SCANFormWizard, self).__setup__()
        self._error_messages.update({
            'scan_form_error': '"%s"',
            'wrong_carrier': 'Carrier for selected shipment is not Endicia'
        })

    def default_make_scanform(self, data):
        """
        Generate the SCAN Form for the current shipment record
        """
        Shipment = Pool().get('stock.shipment.out')
        EndiciaConfiguration = Pool().get('endicia.configuration')
        Attachment = Pool().get('ir.attachment')

        # Getting the api credentials to be used in refund request generation
        # endget_weight_for_endiciaicia credentials are in the format :
        # (account_id, requester_id, passphrase, is_test)
        endicia_credentials = EndiciaConfiguration(1).get_endicia_credentials()
        default = {}

        shipments = Shipment.browse(Transaction().context['active_ids'])

        pic_numbers = []
        for shipment in shipments:
            if not (
                shipment.carrier and
                shipment.carrier.carrier_cost_method == 'endicia'
            ):
                self.raise_user_error('wrong_carrier')

            pic_numbers.append(shipment.tracking_number)

        test = endicia_credentials.is_test and 'Y' or 'N'

        scan_request = SCANFormAPI(
            pic_numbers=pic_numbers,
            accountid=endicia_credentials.account_id,
            requesterid=endicia_credentials.requester_id,
            passphrase=endicia_credentials.passphrase,
            test=test,
        )
        response = scan_request.send_request()
        result = objectify_response(response)
        if not hasattr(result, 'SCANForm'):
            default['response'] = result.ErrorMsg
        else:
            Attachment.create([{
                'name': 'SCAN%s.png' % str(result.SubmissionID),
                'data': buffer(base64.decodestring(result.SCANForm.pyval)),
                'resource': 'stock.shipment.out,%s' % shipment.id
            }])
            default['response'] = 'SCAN' + str(result.SubmissionID)
        return default


class BuyPostageWizardView(ModelView):
    """Buy Postage Wizard View
    """
    __name__ = 'buy.postage.wizard.view'

    company = fields.Many2One('company.company', 'Company', required=True)
    amount = fields.Numeric('Amount in USD', required=True)
    response = fields.Text('Response', readonly=True)

    @staticmethod
    def default_company():
        return Transaction().context.get('company')


class BuyPostageWizard(Wizard):
    """Buy Postage Wizard
    """
    __name__ = 'buy.postage.wizard'

    start = StateView(
        'buy.postage.wizard.view',
        'endicia_integration.endicia_buy_postage_wizard_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Buy Postage', 'buy_postage', 'tryton-ok'),
        ]
    )
    buy_postage = StateView(
        'buy.postage.wizard.view',
        'endicia_integration.endicia_buy_postage_wizard_view_form', [
            Button('OK', 'end', 'tryton-ok'),
        ]
    )

    def default_buy_postage(self, data):
        """
        Generate the SCAN Form for the current shipment record
        """
        EndiciaConfiguration = Pool().get('endicia.configuration')

        default = {}
        endicia_credentials = EndiciaConfiguration(1).get_endicia_credentials()

        buy_postage_api = BuyingPostageAPI(
            request_id=Transaction().user,
            recredit_amount=self.start.amount,
            requesterid=endicia_credentials.requester_id,
            accountid=endicia_credentials.account_id,
            passphrase=endicia_credentials.passphrase,
            test=endicia_credentials.is_test,
        )
        response = buy_postage_api.send_request()

        result = objectify_response(response)
        default['company'] = self.start.company
        default['amount'] = self.start.amount
        default['response'] = str(result.ErrorMessage) \
            if hasattr(result, 'ErrorMessage') else 'Success'
        return default


class StockMove:
    "Stock move"
    __name__ = "stock.move"

    @classmethod
    def __setup__(cls):
        super(StockMove, cls).__setup__()
        cls._error_messages.update({
            'weight_required':
                'Weight for product %s in stock move is missing',
        })

    def get_weight_for_endicia(self):
        """
        Returns weight as required for endicia.

        Upward rounded integral values in Oz
        """
        ProductUom = Pool().get('product.uom')

        if self.product.type == 'service':
            return 0

        if not self.product.weight:
            self.raise_user_error(
                'weight_required',
                error_args=(self.product.name,)
            )

        # Find the quantity in the default uom of the product as the weight
        # is for per unit in that uom
        if self.uom != self.product.default_uom:
            quantity = ProductUom.compute_qty(
                self.uom,
                self.quantity,
                self.product.default_uom
            )
        else:
            quantity = self.quantity

        weight = float(self.product.weight) * quantity

        # Endicia by default uses oz for weight purposes
        if self.product.weight_uom.symbol != 'oz':
            ounce, = ProductUom.search([('symbol', '=', 'oz')])
            weight = ProductUom.compute_qty(
                self.product.weight_uom,
                weight,
                ounce
            )
        return math.ceil(weight)
