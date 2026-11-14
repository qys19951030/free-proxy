import unittest
import threading
import time
from unittest.mock import MagicMock, patch

import requests

from fp.errors import FreeProxyException
from fp.pool import ProxyPool
from fp.sources import FIELDS, SOURCES, parse_proxy_list

URLS = {source['id']: source['url'] for source in SOURCES}


def make_page(rows, header=True):
    '''Build a list page like the source sites serve: a table#list whose
    first row is a header, followed by one <tr> per proxy row.'''
    head = '<tr><th>IP Address</th><th>Port</th></tr>' if header else ''
    body = ''.join(
        '<tr>' + ''.join(f'<td>{cell}</td>' for cell in row) + '</tr>'
        for row in rows)
    return f'<html><body><table id="list">{head}{body}</table></body></html>'.encode()


def make_row(ip='1.2.3.4', port='8080', country='US', anonymity='anonymous',
             google='no', https='yes'):
    '''One row in the column layout shared by all four default sources.'''
    return [ip, port, country, 'Country name', anonymity, google, https,
            '1 min ago']


class TestSourcesConfig(unittest.TestCase):

    def test_four_sources_with_required_keys(self):
        self.assertEqual(4, len(SOURCES))
        ids = [source['id'] for source in SOURCES]
        self.assertEqual(['sslproxies', 'us-proxy', 'uk-proxy',
                          'free-proxy-list'], ids)
        for source in SOURCES:
            self.assertTrue(source['url'].startswith('https://'))
            self.assertIn('list', source['table_xpath'])
            self.assertGreater(source['timeout'], 0)
            for field in FIELDS:
                self.assertIn(field, source['columns'])

    def test_urls_match_freeproxy_website_rules(self):
        urls = {source['id']: source['url'] for source in SOURCES}
        self.assertEqual('https://www.sslproxies.org', urls['sslproxies'])
        self.assertEqual('https://www.us-proxy.org', urls['us-proxy'])
        self.assertEqual('https://free-proxy-list.net/uk-proxy.html',
                         urls['uk-proxy'])
        self.assertEqual('https://free-proxy-list.net', urls['free-proxy-list'])


class TestParseProxyList(unittest.TestCase):

    def test_parse_extracts_fields_by_column_mapping(self):
        rows, bad_rows = parse_proxy_list(
            make_page([make_row(ip='9.9.9.9', port='3128', country='NL',
                                anonymity='elite proxy', google='yes',
                                https='no')]), SOURCES[0])
        self.assertEqual(0, bad_rows)
        self.assertEqual([{'ip': '9.9.9.9', 'port': '3128', 'country': 'NL',
                           'anonymity': 'elite proxy', 'google': 'yes',
                           'https': 'no'}], rows)

    def test_parse_skips_header_row(self):
        rows, bad_rows = parse_proxy_list(make_page([make_row()]), SOURCES[0])
        self.assertEqual(1, len(rows))
        self.assertEqual(0, bad_rows)

    def test_row_with_too_few_columns_is_dropped(self):
        page = make_page([make_row(), ['1.1.1.1', '80'], make_row()])
        rows, bad_rows = parse_proxy_list(page, SOURCES[0])
        self.assertEqual(2, len(rows))
        self.assertEqual(1, bad_rows)

    def test_row_with_empty_ip_or_port_is_dropped(self):
        page = make_page([make_row(ip=''), make_row(port=''), make_row()])
        rows, bad_rows = parse_proxy_list(page, SOURCES[0])
        self.assertEqual(1, len(rows))
        self.assertEqual(2, bad_rows)

    def test_parse_uses_source_column_mapping(self):
        '''A source with a different layout only needs a different config.'''
        source = {'id': 'custom', 'url': 'https://example.com',
                  'table_xpath': '//*[@id="list"]//tr',
                  'columns': {'ip': 1, 'port': 0, 'country': 2,
                              'anonymity': 3, 'google': 4, 'https': 5},
                  'timeout': 5}
        page = make_page([['8080', '5.6.7.8', 'BR', 'transparent', 'no',
                           'yes']])
        rows, bad_rows = parse_proxy_list(page, source)
        self.assertEqual(0, bad_rows)
        self.assertEqual('5.6.7.8', rows[0]['ip'])
        self.assertEqual('8080', rows[0]['port'])
        self.assertEqual('transparent', rows[0]['anonymity'])


def default_pages():
    '''One list page per source URL, each with a distinct proxy row.'''
    return {
        URLS['sslproxies']: make_page([
            make_row(ip='1.1.1.1', port='80', country='US',
                     anonymity='elite proxy', google='no', https='yes'),
            make_row(ip='2.2.2.2', port='8080', country='NL',
                     anonymity='anonymous', google='yes', https='no')]),
        URLS['us-proxy']: make_page([
            make_row(ip='3.3.3.3', port='3128', country='US',
                     anonymity='anonymous', google='no', https='yes')]),
        URLS['uk-proxy']: make_page([
            make_row(ip='4.4.4.4', port='80', country='GB',
                     anonymity='transparent', google='no', https='no')]),
        URLS['free-proxy-list']: make_page([
            make_row(ip='1.1.1.1', port='80', country='',
                     anonymity='anonymous', google='yes', https='no'),
            make_row(ip='5.5.5.5', port='1080', country='BR',
                     anonymity='elite proxy', google='yes', https='yes')]),
    }


def fake_get_factory(pages, errors=None):
    errors = errors or {}

    def fake_get(url, timeout=None):
        if url in errors:
            raise errors[url]
        response = MagicMock()
        response.content = pages[url]
        return response
    return fake_get


def working_check(testcase=None):
    '''A check_proxy_is_working stand-in where every proxy works.'''
    return MagicMock(side_effect=lambda url, schema, timeout, address:
                     f'http://{address}')


class TestPoolFetch(unittest.TestCase):

    @patch('fp.pool.requests.get')
    def test_all_sources_fetched_each_round(self, mock_get):
        mock_get.side_effect = fake_get_factory(default_pages())
        pool = ProxyPool()
        pool._refresh_if_stale()
        fetched_urls = [call.args[0] for call in mock_get.call_args_list]
        self.assertEqual(sorted(URLS.values()), sorted(fetched_urls))

    @patch('fp.pool.requests.get')
    def test_source_config_timeout_used_by_default(self, mock_get):
        mock_get.side_effect = fake_get_factory(default_pages())
        ProxyPool()._refresh_if_stale()
        for call in mock_get.call_args_list:
            self.assertEqual(10, call.kwargs['timeout'])

    @patch('fp.pool.requests.get')
    def test_request_timeout_overrides_source_timeout(self, mock_get):
        mock_get.side_effect = fake_get_factory(default_pages())
        ProxyPool(request_timeout=3)._refresh_if_stale()
        for call in mock_get.call_args_list:
            self.assertEqual(3, call.kwargs['timeout'])

    @patch('fp.pool.requests.get')
    def test_failed_source_does_not_affect_others(self, mock_get):
        import requests
        mock_get.side_effect = fake_get_factory(
            default_pages(),
            errors={URLS['us-proxy']: requests.exceptions.ConnectionError()})
        pool = ProxyPool()
        pool._refresh_if_stale()
        reports = {r['id']: r for r in pool.report()['sources']}
        self.assertFalse(reports['us-proxy']['ok'])
        self.assertIn('us-proxy.org', reports['us-proxy']['error'])
        for source_id in ('sslproxies', 'uk-proxy', 'free-proxy-list'):
            self.assertTrue(reports[source_id]['ok'])
            self.assertIsNone(reports[source_id]['error'])
            self.assertGreater(reports[source_id]['elapsed_ms'], -1)
        # Candidates from the three healthy sources are still merged.
        self.assertEqual(4, len(pool._cache))

    @patch('fp.pool.requests.get')
    def test_bad_rows_counted_per_source(self, mock_get):
        pages = default_pages()
        pages[URLS['sslproxies']] = make_page(
            [make_row(ip='1.1.1.1', port='80'), ['9.9.9.9'], make_row(port='')])
        mock_get.side_effect = fake_get_factory(pages)
        pool = ProxyPool()
        pool._refresh_if_stale()
        reports = {r['id']: r for r in pool.report()['sources']}
        self.assertEqual(2, reports['sslproxies']['bad_rows'])
        self.assertEqual(1, reports['sslproxies']['candidates'])


class TestPoolMerge(unittest.TestCase):

    def merged_by_address(self, pages):
        with patch('fp.pool.requests.get') as mock_get:
            mock_get.side_effect = fake_get_factory(pages)
            pool = ProxyPool()
            pool._refresh_if_stale()
        return {entry['address']: entry for entry in pool._cache}

    def test_duplicate_proxy_merged_with_combined_annotations(self):
        merged = self.merged_by_address(default_pages())
        # 1.1.1.1:80 appears on sslproxies and free-proxy-list.
        self.assertEqual(5, len(merged))
        entry = merged['1.1.1.1:80']
        self.assertEqual(['sslproxies', 'free-proxy-list'], entry['sources'])
        # Strongest anonymity wins (elite over anonymous).
        self.assertEqual('elite', entry['anonymity'])
        # https/google: a single yes from any source is enough.
        self.assertTrue(entry['https'])
        self.assertTrue(entry['google'])
        # First non-empty country wins.
        self.assertEqual('US', entry['country'])

    def test_same_source_duplicate_counts_once(self):
        pages = default_pages()
        pages[URLS['us-proxy']] = make_page([
            make_row(ip='3.3.3.3', port='3128', anonymity='transparent'),
            make_row(ip='3.3.3.3', port='3128', anonymity='elite proxy')])
        merged = self.merged_by_address(pages)
        entry = merged['3.3.3.3:3128']
        self.assertEqual(['us-proxy'], entry['sources'])
        self.assertEqual('elite', entry['anonymity'])

    def test_anonymity_falls_back_to_transparent(self):
        pages = default_pages()
        pages[URLS['uk-proxy']] = make_page([
            make_row(ip='4.4.4.4', port='80', anonymity='unknown')])
        merged = self.merged_by_address(pages)
        self.assertEqual('transparent', merged['4.4.4.4:80']['anonymity'])


class TestPoolFilters(unittest.TestCase):

    def cached_addresses(self, **kwargs):
        with patch('fp.pool.requests.get') as mock_get:
            mock_get.side_effect = fake_get_factory(default_pages())
            pool = ProxyPool(**kwargs)
            pool._refresh_if_stale()
        return {entry['address'] for entry in pool._cache}

    def test_country_id_filters_merged_country(self):
        self.assertEqual({'1.1.1.1:80', '3.3.3.3:3128'},
                         self.cached_addresses(country_id=['US']))

    def test_anonym_filter(self):
        self.assertEqual({'2.2.2.2:8080', '3.3.3.3:3128'},
                         self.cached_addresses(anonym=True))

    def test_elite_filter(self):
        self.assertEqual({'1.1.1.1:80', '5.5.5.5:1080'},
                         self.cached_addresses(elite=True))

    def test_google_filter(self):
        self.assertEqual({'1.1.1.1:80', '2.2.2.2:8080', '5.5.5.5:1080'},
                         self.cached_addresses(google=True))
        self.assertEqual({'3.3.3.3:3128', '4.4.4.4:80'},
                         self.cached_addresses(google=False))

    def test_https_filter(self):
        self.assertEqual({'1.1.1.1:80', '3.3.3.3:3128', '5.5.5.5:1080'},
                         self.cached_addresses(https=True))


class PoolTestCase(unittest.TestCase):
    '''Base class patching source fetching with the default four pages.'''

    def setUp(self):
        get_patcher = patch('fp.pool.requests.get')
        self.mock_get = get_patcher.start()
        self.addCleanup(get_patcher.stop)
        self.mock_get.side_effect = fake_get_factory(default_pages())
        check_patcher = patch('fp.pool.check_proxy_is_working')
        self.mock_check = check_patcher.start()
        self.addCleanup(check_patcher.stop)
        self.mock_check.side_effect = (
            lambda url, schema, timeout, address: f'http://{address}')

    def make_pool(self, **kwargs):
        return ProxyPool(**kwargs)


class TestPoolGet(PoolTestCase):

    def test_get_returns_records_with_expected_keys(self):
        records = self.make_pool().get(2)
        self.assertEqual(2, len(records))
        for record in records:
            self.assertTrue(record['proxy'].startswith('http://'))
            self.assertIsInstance(record['sources'], list)
            self.assertIn(record['anonymity'],
                          ('elite', 'anonymous', 'transparent'))
            self.assertIsInstance(record['https'], bool)
            self.assertIsInstance(record['google'], bool)
            self.assertIsInstance(record['country'], str)
            self.assertIsNotNone(record['latency_ms'])

    def test_get_stops_after_n_successes(self):
        pool = self.make_pool(max_concurrent=1)
        records = pool.get(2)
        self.assertEqual(2, len(records))
        self.assertEqual(2, pool.report()['checked'])
        self.assertFalse(pool.report()['exhausted'])

    def test_get_returns_partial_and_sets_exhausted(self):
        def check(url, schema, timeout, address):
            if address.startswith('1.') or address.startswith('2.'):
                return f'http://{address}'
            raise requests.exceptions.ConnectionError()
        self.mock_check.side_effect = check
        pool = self.make_pool()
        records = pool.get(5)
        self.assertEqual(2, len(records))
        self.assertTrue(pool.report()['exhausted'])

    def test_get_raises_only_when_nothing_works(self):
        self.mock_check.side_effect = requests.exceptions.ConnectionError()
        pool = self.make_pool()
        with self.assertRaisesRegex(FreeProxyException,
                                    'There are no working proxies at this time.'):
            pool.get(3)

    def test_all_sources_failed_message_names_sources_and_reasons(self):
        self.mock_get.side_effect = requests.exceptions.ConnectionError()
        pool = self.make_pool()
        with self.assertRaises(FreeProxyException) as ctx:
            pool.get()
        message = ctx.exception.message
        self.assertIn('There are no working proxies at this time.', message)
        for source_id in URLS:
            self.assertIn(source_id, message)
        self.assertIn('Request to https://www.us-proxy.org failed', message)

    def test_get_requires_positive_n(self):
        with self.assertRaisesRegex(ValueError, 'positive integer'):
            self.make_pool().get(0)

    def test_invalid_max_concurrent_raises(self):
        with self.assertRaisesRegex(ValueError, 'positive integer'):
            ProxyPool(max_concurrent=0)


class TestPoolOrdering(unittest.TestCase):

    def pool_with_verified_cache(self, entries):
        pool = ProxyPool()
        pool._cache = [entry for entry, _latency in entries]
        pool._cache_fetched_at = time.monotonic()
        pool._checks = {entry['address']: {'ok': True, 'latency_ms': latency,
                                           'checked_at': 1, 'error': None}
                        for entry, latency in entries}
        return pool

    @staticmethod
    def entry(address, anonymity='anonymous', https=False, google=False,
              country='US', sources=('sslproxies',)):
        return {'address': address, 'sources': list(sources),
                'anonymity': anonymity, 'https': https, 'google': google,
                'country': country}

    def test_order_anonymity_then_https_then_latency_then_address(self):
        pool = self.pool_with_verified_cache([
            (self.entry('9.9.9.9:1', anonymity='transparent', https=True), 1),
            (self.entry('2.2.2.2:2', anonymity='elite', https=False), 10),
            (self.entry('1.1.1.1:1', anonymity='elite', https=True), 50),
            (self.entry('3.3.3.3:3', anonymity='elite', https=True), 20),
            (self.entry('4.4.4.4:4', anonymity='anonymous', https=True), 5),
        ])
        records = pool.get(5)
        self.assertEqual(['http://3.3.3.3:3', 'http://1.1.1.1:1',
                          'http://2.2.2.2:2', 'http://4.4.4.4:4',
                          'http://9.9.9.9:1'],
                         [record['proxy'] for record in records])

    def test_order_falls_back_to_address_for_equal_latency(self):
        pool = self.pool_with_verified_cache([
            (self.entry('2.2.2.2:2', anonymity='elite', https=True), 10),
            (self.entry('1.1.1.1:1', anonymity='elite', https=True), 10),
        ])
        records = pool.get(2)
        self.assertEqual(['http://1.1.1.1:1', 'http://2.2.2.2:2'],
                         [record['proxy'] for record in records])

    def test_rand_shuffles_candidates(self):
        entries = [self.entry(f'1.1.1.{i}:80') for i in range(1, 6)]
        pool = self.pool_with_verified_cache(
            [(entry, 1) for entry in entries])
        pool.random = True
        with patch('fp.pool.random.shuffle',
                   side_effect=lambda items: items.reverse()) as mock_shuffle:
            records = pool.get(5)
        mock_shuffle.assert_called()
        self.assertEqual(['http://1.1.1.5:80', 'http://1.1.1.4:80',
                          'http://1.1.1.3:80', 'http://1.1.1.2:80',
                          'http://1.1.1.1:80'],
                         [record['proxy'] for record in records])


class TestPoolCacheAndCooldown(PoolTestCase):

    def test_second_get_within_ttl_reuses_cache_and_checks(self):
        pool = self.make_pool()
        first = pool.get(2)
        self.assertEqual(4, self.mock_get.call_count)
        checked = self.mock_check.call_count
        second = pool.get(2)
        self.assertEqual(first, second)
        self.assertEqual(4, self.mock_get.call_count)
        self.assertEqual(checked, self.mock_check.call_count)

    def test_expired_cache_refetches_sources(self):
        pool = self.make_pool(cache_ttl=0)
        pool.get(1)
        pool.get(1)
        self.assertEqual(8, self.mock_get.call_count)

    def test_failed_proxy_enters_cooldown_and_is_skipped(self):
        def check(url, schema, timeout, address):
            if address == '2.2.2.2:8080':
                raise requests.exceptions.ConnectionError()
            return f'http://{address}'
        self.mock_check.side_effect = check
        pool = self.make_pool()
        pool.get(5)
        self.assertIn('2.2.2.2:8080', pool._cooldowns)
        self.assertEqual('connection_error',
                         pool._checks['2.2.2.2:8080']['error'])
        calls = self.mock_check.call_count
        pool.get(5)
        self.assertEqual(calls, self.mock_check.call_count)
        self.assertEqual(1, pool.report()['cooldown_skipped'])

    def test_cooldown_expiry_returns_proxy_to_candidates(self):
        def check(url, schema, timeout, address):
            if address == '2.2.2.2:8080':
                raise requests.exceptions.ConnectionError()
            return f'http://{address}'
        self.mock_check.side_effect = check
        pool = self.make_pool(retries=0)
        pool.get(5)
        pool._cooldowns['2.2.2.2:8080'] = time.monotonic() - 1
        pool.get(5)
        self.assertEqual(0, pool.report()['cooldown_skipped'])
        # The expired proxy was checked again (once per round, retries=0).
        attempts = sum(1 for call in self.mock_check.call_args_list
                       if call.args[3] == '2.2.2.2:8080')
        self.assertEqual(2, attempts)

    def test_timeout_and_peer_mismatch_categories(self):
        def check(url, schema, timeout, address):
            if address == '2.2.2.2:8080':
                raise requests.exceptions.Timeout()
            if address == '3.3.3.3:3128':
                return None
            return f'http://{address}'
        self.mock_check.side_effect = check
        pool = self.make_pool()
        pool.get(5)
        self.assertEqual('timeout', pool._checks['2.2.2.2:8080']['error'])
        self.assertEqual('peer_mismatch',
                         pool._checks['3.3.3.3:3128']['error'])

    def test_connection_errors_are_retried_before_cooldown(self):
        self.mock_check.side_effect = requests.exceptions.ConnectionError()
        pool = self.make_pool(retries=2)
        with self.assertRaises(FreeProxyException):
            pool.get(1)
        for call in self.mock_check.call_args_list:
            self.assertEqual(4, len(call.args))
        # Every candidate got 1 initial attempt + 2 retries.
        per_address = {}
        for call in self.mock_check.call_args_list:
            per_address[call.args[3]] = per_address.get(call.args[3], 0) + 1
        self.assertTrue(all(count == 3 for count in per_address.values()))

    def test_peer_mismatch_is_not_retried(self):
        self.mock_check.side_effect = (
            lambda url, schema, timeout, address: None)
        pool = self.make_pool(retries=2)
        with self.assertRaises(FreeProxyException):
            pool.get(1)
        per_address = {}
        for call in self.mock_check.call_args_list:
            per_address[call.args[3]] = per_address.get(call.args[3], 0) + 1
        self.assertTrue(all(count == 1 for count in per_address.values()))


class TestPoolConcurrency(PoolTestCase):

    def test_max_concurrent_limits_parallel_checks(self):
        running = 0
        max_running = 0
        lock = threading.Lock()

        def check(url, schema, timeout, address):
            nonlocal running, max_running
            with lock:
                running += 1
                max_running = max(max_running, running)
            time.sleep(0.02)
            with lock:
                running -= 1
            return f'http://{address}'
        self.mock_check.side_effect = check
        self.make_pool(max_concurrent=2).get(5)
        self.assertEqual(2, max_running)

    def test_concurrent_gets_share_a_single_refresh(self):
        barrier = threading.Barrier(2)

        def slow_get(url, timeout=None):
            time.sleep(0.05)
            response = MagicMock()
            response.content = default_pages()[url]
            return response
        self.mock_get.side_effect = slow_get
        pool = self.make_pool()
        results = []

        def worker():
            barrier.wait()
            results.append(pool.get(1))
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(2, len(results))
        # One refresh round of four sources, not two.
        self.assertEqual(4, self.mock_get.call_count)


class TestPoolReport(PoolTestCase):

    def test_report_structure(self):
        pool = self.make_pool()
        pool.get(2)
        report = pool.report()
        self.assertEqual([source['id'] for source in SOURCES],
                         [r['id'] for r in report['sources']])
        for source_report in report['sources']:
            self.assertTrue(source_report['ok'])
            self.assertIsNone(source_report['error'])
            self.assertIsInstance(source_report['elapsed_ms'], float)
            self.assertGreaterEqual(source_report['candidates'], 0)
            self.assertGreaterEqual(source_report['bad_rows'], 0)
        self.assertEqual(2, report['checked'])
        self.assertEqual(0, report['cooldown_skipped'])
        self.assertFalse(report['exhausted'])


if __name__ == '__main__':
    unittest.main()
