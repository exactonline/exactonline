# vim: set ts=8 sw=4 sts=4 et ai tw=79:
"""
Data structures for communication with remote.

Example usage::

    class BossoExactInvoice(ExactInvoice):
        def __init__(self, bosso_invoice=None, **kwargs):
            super(BossoExactInvoice, self).__init__(**kwargs)

            self._bosso_invoice = bosso_invoice

        def get_customer(self):
            return BossoExactCustomer(
                bosso_relation=self._bosso_invoice.relation, api=self._api)

        def get_created_data(self):
            return self._bosso_invoice.open_date

        # ...

This file is part of the Exact Online REST API Library in Python
(EORALP), licensed under the LGPLv3+.
Copyright (C) 2015 Walter Doekes, OSSO B.V.
"""
from warnings import warn

from .base import ExactElement
from ..exceptions import ExactOnlineError, ObjectDoesNotExist


class ExactInvoice(ExactElement):
    def get_guid(self):
        exact_invoice = self._api.invoices.get(
            invoice_number=self.get_invoice_number())
        return exact_invoice['EntryID']

    def get_customer(self):
        # The ExactOnline customer id.
        raise NotImplementedError()

    def get_created_date(self):
        # Period used for both EntryDate and ReportingPeriod. Use the
        # "open_date" if available.
        return NotImplementedError()

    def get_exact_journal(self):
        # E.g. "70" for "Verkoopboek"
        raise NotImplementedError()

    def get_ledger_lines(self):
        # Return a bunch of ledger lines, like this:
        # {'code': '1234',          # ledger code
        #  'vat_percentage': '21',  # 21%
        #  'total_amount_excl_vat': Decimal(12.5),
        #  'description': '200 items of foo bar'}
        raise NotImplementedError()

    def get_invoice_number(self):
        raise NotImplementedError()

    def get_total_amount_incl_vat(self):
        # Used in AmountDC (default currency) and AmountFC (foreign
        # currency).
        raise NotImplementedError()

    def get_total_vat(self):
        raise NotImplementedError()

    def hint_exact_invoice_number(self):
        # Exact does not honor all requests.
        raise NotImplementedError()

    def assemble(self):
        invoice_number = self.get_invoice_number()
        customer = self.get_customer()

        total_amount_incl_vat = self.get_total_amount_incl_vat()
        total_vat = self.get_total_vat()
        created_date = self.get_created_date()
        description = u'%s - %s, %s' % (invoice_number, customer.get_name(),
                                        created_date.strftime('%m-%Y'))

        # Make sure the customer exists.
        try:
            customer_guid = customer.get_guid()
        except ObjectDoesNotExist:
            customer.commit()
            customer_guid = customer.get_guid()

        # Compile data to send.
        data = {
            # Converting to string is better than converting to float.
            'AmountDC': str(total_amount_incl_vat),  # DC=default_currency
            'AmountFC': str(total_amount_incl_vat),  # FC=foreign_currency

            # Strange! We receive the date(time) objects as
            # '/Date(unixmilliseconds)/' (mktime(d.timetuple())*1000),
            # but we must send them as ISO8601.
            # NOTE: invoice.open_date is a date, not a datetime, so
            # tzinfo calculations won't work on it, and we cannot use
            # '%z' in the strftime format. Unused code:
            #   import pytz; tzinfo = pytz.timezone('Europe/Amsterdam')
            #   entry_date = tzinfo.localize(invoice.open_date)
            # Pretend we're in UTC and send "Z" zone.
            'EntryDate': created_date.strftime('%Y-%m-%dT%H:%M:%SZ'),

            'Customer': customer_guid,
            'Description': description,
            'Journal': self.get_exact_journal(),
            'ReportingPeriod': created_date.month,
            'ReportingYear': created_date.year,
            'SalesEntryLines': [],
            'VATAmountDC': str(total_vat),  # str>float, DC=default_currency
            'VATAmountFC': str(total_vat),  # str>float, FC=foreign_currency
            'YourRef': invoice_number,

            'InvoiceNumber': self.hint_exact_invoice_number(),
        }

        # Fetch ledger lines.
        ledger_lines = self.get_ledger_lines()

        # Cache ledger codes to ledger GUIDs.
        if ledger_lines:
            assert isinstance(ledger_lines[0]['code'], basestring)
            ledger_ids = self._api.ledgeraccounts.filter(
                code__in=set([i['code'] for i in ledger_lines]))
            ledger_ids = dict((unicode(i['Code']), i['ID'])
                              for i in ledger_ids)

        for ledger_line in self.get_ledger_lines():
            try:
                ledger_id = ledger_ids[ledger_line['code']]
            except KeyError:
                raise ExactOnlineError(
                    'Cannot submit invoice with ledger code %s' %
                    (ledger_line['code'],))

            # We must use VATCode. It accepts VATPercentages, but only
            # when it is higher than 0. Not using:
            # 'VATPercentage': str(ledger_line['vat_percentage'] / 100)
            if ledger_line['vat_percentage'] == 0:
                vatcode = '0  '  # FIXME: hardcoded.. fetch from API?
            elif ledger_line['vat_percentage'] == 21:
                vatcode = '2  '  # FIXME: hardcoded.. fetch from API?
            else:
                raise NotImplementedError(
                    'Unknown VAT: %s' % (ledger_line['vat_percentage'],))

            # Again, convert from decimal to str to get more precision.
            line = {'AmountDC': str(ledger_line['total_amount_excl_vat']),
                    'AmountFC': str(ledger_line['total_amount_excl_vat']),
                    'Description': ledger_line['description'],
                    'GLAccount': ledger_id,
                    'VATCode': vatcode}
            data['SalesEntryLines'].append(line)

        return data

    def commit(self):
        try:
            exact_guid = self.get_guid()
        except ObjectDoesNotExist:
            exact_guid = None

        data = self.assemble()

        if exact_guid:
            # We cannot supply the lines on PUT/update directly.
            salesentrylines = data.pop('SalesEntryLines')
            # Update the invoice.
            ret = self._api.invoices.update(exact_guid, data)
            # FIXME: At this point we should compare and fix the
            # salesentrylines.
            # Example:
            # > line_data = {'AmountFC': '-0.01', 'AmountDC': '-0.01',
            # >              'EntryID': exact_guid, 'GLAccount': '6d28...'}
            # > self._api.restv1('POST', 'salesentry/SalesEntryLines', line_data)
            # Example:
            # > inv._api.restv1('DELETE', "salesentry/SalesEntryLines" +
            # >                           "(guid'0481...')")
            warn('PUT of SalesEntry SalesEntryLines is not supported yet!')
            del salesentrylines
            # ret is None
        else:
            ret = self._api.invoices.create(data)
            # ret is a exact_invoice

        return ret
