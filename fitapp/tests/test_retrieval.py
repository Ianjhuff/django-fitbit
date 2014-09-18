from __future__ import absolute_import

import celery
import json
import sys

from dateutil import parser
from django.core.cache import cache
from django.core.urlresolvers import reverse
from mock import MagicMock, patch

from fitbit import exceptions as fitbit_exceptions
from fitbit.api import Fitbit

from fitapp import utils
from fitapp.models import UserFitbit, TimeSeriesData, TimeSeriesDataType
from fitapp.tasks import get_time_series_data

from .base import FitappTestBase


class TestRetrievalUtility(FitappTestBase):
    """Tests for the get_fitbit_data utility function."""

    def setUp(self):
        super(TestRetrievalUtility, self).setUp()
        self.period = '30d'
        self.base_date = '2012-06-01'
        self.end_date = None

    @patch.object(Fitbit, 'time_series')
    def _mock_time_series(self, time_series=None, error=None, response=None,
                          error_attrs={}):
        if error:
            exc = error(self._error_response())
            for k, v in error_attrs.items():
                setattr(exc, k, v)
            time_series.side_effect = exc
        elif response:
            time_series.return_value = response
        resource_type = TimeSeriesDataType.objects.get(
            category=TimeSeriesDataType.activities, resource='steps')
        return utils.get_fitbit_data(
            self.fbuser, resource_type, base_date=self.base_date,
            period=self.period, end_date=self.end_date)

    def _error_test(self, error):
        with self.assertRaises(error):
            self._mock_time_series(error=error)

    def test_value_error(self):
        """ValueError from the Fitbit.time_series should propagate."""
        self._error_test(ValueError)

    def test_type_error(self):
        """TypeError from the Fitbit.time_series should propagate."""
        self._error_test(TypeError)

    def test_unauthorized(self):
        """HTTPUnauthorized from the Fitbit.time_series should propagate."""
        self._error_test(fitbit_exceptions.HTTPUnauthorized)

    def test_forbidden(self):
        """HTTPForbidden from the Fitbit.time_series should propagate."""
        self._error_test(fitbit_exceptions.HTTPForbidden)

    def test_not_found(self):
        """HTTPNotFound from the Fitbit.time_series should propagate."""
        self._error_test(fitbit_exceptions.HTTPNotFound)

    def test_conflict(self):
        """HTTPConflict from the Fitbit.time_series should propagate."""
        self._error_test(fitbit_exceptions.HTTPConflict)

    def test_server_error(self):
        """HTTPServerError from the Fitbit.time_series should propagate."""
        self._error_test(fitbit_exceptions.HTTPServerError)

    def test_bad_request(self):
        """HTTPBadRequest from the Fitbit.time_series should propagate."""
        self._error_test(fitbit_exceptions.HTTPBadRequest)

    def test_too_many_requests(self):
        """HTTPTooManyRequests from the Fitbit.time_series should propagate."""
        try:
            self._mock_time_series(error=fitbit_exceptions.HTTPTooManyRequests,
                                   error_attrs={'retry_after_secs': 35})
        except fitbit_exceptions.HTTPTooManyRequests:
            self.assertEqual(sys.exc_info()[1].retry_after_secs, 35)
        else:
            assert False, 'Should have thrown exception'

    def test_retrieval(self):
        """get_fitbit_data should return a list of daily steps data."""
        response = {'activities-steps': [1, 2, 3]}
        steps = self._mock_time_series(response=response)
        self.assertEqual(steps, response['activities-steps'])


class TestRetrievalTask(FitappTestBase):
    def setUp(self):
        super(TestRetrievalTask, self).setUp()
        self.category = 'activities'
        self.date = '2013-05-02'
        self.value = 10

    def _receive_fitbit_updates(self):
        updates = json.dumps([{
            u'subscriptionId': self.fbuser.user.id,
            u'ownerId': self.fbuser.fitbit_user,
            u'collectionType': self.category,
            u'date': self.date
        }])
        res = self.client.post(reverse('fitbit-update'), data=updates,
                               content_type='multipart/form-data')
        assert res.status_code, 204

    @patch('fitapp.utils.get_fitbit_data')
    def test_subscription_update(self, get_fitbit_data):
        # Check that celery tasks get made when a notification is received
        # from Fitbit.
        get_fitbit_data.return_value = [{'value': self.value}]
        category = getattr(TimeSeriesDataType, self.category)
        resources = TimeSeriesDataType.objects.filter(category=category)
        self._receive_fitbit_updates()
        self.assertEqual(get_fitbit_data.call_count, resources.count())
        # Check that the cache locks have been deleted
        for resource in resources:
            self.assertEqual(
                cache.get('fitapp.get_time_series_data-lock-%s-%s-%s' % (
                    category, resource.resource, self.date)
                ), None)
        date = parser.parse(self.date)
        for tsd in TimeSeriesData.objects.filter(user=self.user, date=date):
            assert tsd.value, self.value

    @patch('fitapp.utils.get_fitbit_data')
    @patch('django.core.cache.cache.add')
    def test_subscription_update_locked(self, mock_add, get_fitbit_data):
        # Check that celery tasks do not get made when a notification is
        # received from Fitbit, but there is already a matching task in
        # progress
        mock_add.return_value = False
        self.assertEqual(TimeSeriesData.objects.count(), 0)
        self._receive_fitbit_updates()
        self.assertEqual(get_fitbit_data.call_count, 0)
        self.assertEqual(TimeSeriesData.objects.count(), 0)

    @patch('fitapp.utils.get_fitbit_data')
    @patch('fitapp.tasks.get_time_series_data.retry')
    def test_subscription_update_too_many(self, mock_retry, get_fitbit_data):
        # Check that celery tasks get postponed if the rate limit is hit
        mock_retry.return_value = celery.exceptions.Retry()
        exc = fitbit_exceptions.HTTPTooManyRequests(self._error_response())
        exc.retry_after_secs = 21
        get_fitbit_data.side_effect = exc
        category = getattr(TimeSeriesDataType, self.category)
        resources = TimeSeriesDataType.objects.filter(category=category)
        self.assertEqual(TimeSeriesData.objects.count(), 0)
        self._receive_fitbit_updates()
        self.assertEqual(get_fitbit_data.call_count, resources.count())
        self.assertEqual(TimeSeriesData.objects.count(), 0)
        mock_retry.assert_called_with(exc, countdown=21)

    def test_problem_queueing_task(self):
        get_time_series_data = MagicMock()
        # If queueing the task raises an exception, it doesn't propagate
        get_time_series_data.delay.side_effect = Exception
        try:
            self._receive_fitbit_updates()
        except:
            assert False, 'Any errors should be captured in the view'


class RetrievalViewTestBase(object):
    """Base methods for the get_steps view."""
    url_name = 'fitbit-steps'
    valid_periods = utils.get_valid_periods()

    def setUp(self):
        super(RetrievalViewTestBase, self).setUp()
        self.period = '30d'
        self.base_date = '2012-06-06'
        self.end_date = '2012-07-07'

    def _check_response(self, response, code, objects=None, error_msg=None):
        objects = objects or []
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content.decode('utf8'))
        self.assertEqual(data['meta']['status_code'], code, error_msg)
        self.assertEqual(data['meta']['total_count'], len(objects),
                         error_msg)
        self.assertEqual(data['objects'], objects, error_msg)

    def test_not_authenticated(self):
        """Status code should be 101 when user isn't logged in."""
        self.client.logout()
        response = self._get(get_kwargs=self._data())
        self._check_response(response, 101)
        self.assertEqual(UserFitbit.objects.count(), 1)

    def test_not_active(self):
        """Status code should be 101 when user isn't active."""
        self.user.is_active = False
        self.user.save()
        response = self._get(get_kwargs=self._data())
        self._check_response(response, 101)
        self.assertEqual(UserFitbit.objects.count(), 1)

    def test_not_integrated(self):
        """Status code should be 102 when user is not integrated."""
        self.fbuser.delete()
        response = self._get(get_kwargs=self._data())
        self._check_response(response, 102)
        self.assertEqual(UserFitbit.objects.count(), 0)

    def test_invalid_credentials_unauthorized(self):
        """
        Status code should be 103 & credentials should be deleted when user
        integration is invalid.
        """
        response = self._mock_utility(get_kwargs=self._data(),
                                      error=fitbit_exceptions.HTTPUnauthorized)
        self._check_response(response, 103)
        self.assertEqual(UserFitbit.objects.count(), 0)

    def test_invalid_credentials_forbidden(self):
        """
        Status code should be 103 & credentials should be deleted when user
        integration is invalid.
        """
        response = self._mock_utility(get_kwargs=self._data(),
                                      error=fitbit_exceptions.HTTPForbidden)
        self._check_response(response, 103)
        self.assertEqual(UserFitbit.objects.count(), 0)

    def test_rate_limited(self):
        """Status code should be 105 when Fitbit rate limit is hit."""
        response = self._mock_utility(get_kwargs=self._data(),
                                      error=fitbit_exceptions.HTTPConflict)
        self._check_response(response, 105)

    def test_fitbit_error(self):
        """Status code should be 106 when Fitbit server error occurs."""
        response = self._mock_utility(get_kwargs=self._data(),
                                      error=fitbit_exceptions.HTTPServerError)
        self._check_response(response, 106)

    def test_405(self):
        """View should not respond to anything but a GET request."""
        url = reverse('fitbit-data', args=['activities', 'steps'])
        for method in (self.client.post, self.client.head,
                       self.client.options, self.client.put,
                       self.client.delete):
            response = method(url)
            self.assertEqual(response.status_code, 405)

    def test_ambiguous(self):
        """Status code should be 104 when both period & end_date are given."""
        data = {'end_date': self.end_date, 'period': self.period,
                'base_date': self.base_date}
        response = self._get(get_kwargs=data)
        self._check_response(response, 104)


class TestRetrievePeriod(RetrievalViewTestBase, FitappTestBase):

    def _data(self):
        return {'base_date': self.base_date, 'period': self.period}

    def test_no_period(self):
        """Status code should be 104 when no period is given."""
        data = self._data()
        data.pop('period')
        response = self._get(get_kwargs=data)
        self._check_response(response, 104)

    def test_bad_period(self):
        """Status code should be 104 when invalid period is given."""
        self.period = 'bad'
        response = self._get(get_kwargs=self._data())
        self._check_response(response, 104)

    def test_no_base_date(self):
        """Base date should be optional for period request."""
        data = self._data()
        data.pop('base_date')
        steps = [{'dateTime': '2000-01-01', 'value': 10}]
        response = self._mock_utility(response=steps, get_kwargs=data)
        self._check_response(response, 100, steps)

    def test_bad_base_date(self):
        """Status code should be 104 when invalid base date is given."""
        self.base_date = 'bad'
        response = self._get(get_kwargs=self._data())
        self._check_response(response, 104)

    def test_period(self):
        steps = [{'dateTime': '2000-01-01', 'value': 10}]
        for period in self.valid_periods:
            self.period = period
            data = self._data()
            response = self._mock_utility(response=steps, get_kwargs=data)
            error_msg = 'Should be able to retrieve data for {0}.'.format(
                self.period)
            self._check_response(response, 100, steps, error_msg)


class TestRetrieveRange(RetrievalViewTestBase, FitappTestBase):

    def _data(self):
        return {'base_date': self.base_date, 'end_date': self.end_date}

    def test_range__no_base_date(self):
        """Status code should be 104 when no base date is given."""
        data = self._data()
        data.pop('base_date')
        response = self._get(get_kwargs=data)
        self._check_response(response, 104)

    def test_range__bad_base_date(self):
        """Status code should be 104 when invalid base date is given."""
        self.base_date = 'bad'
        response = self._get(get_kwargs=self._data())
        self._check_response(response, 104)

    def test_range__no_end_date(self):
        """Status code should be 104 when no end date is given."""
        data = self._data()
        data.pop('end_date')
        response = self._get(get_kwargs=data)
        self._check_response(response, 104)

    def test_range__bad_end_date(self):
        """Status code should be 104 when invalid end date is given."""
        self.end_date = 'bad'
        response = self._get(get_kwargs=self._data())
        self._check_response(response, 104)

    def test_range(self):
        steps = [{'dateTime': '2000-01-01', 'value': 10}]
        response = self._mock_utility(response=steps,
                                      get_kwargs=self._data())
        self._check_response(response, 100, steps)
