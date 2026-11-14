import unittest

from fp.sources import FIELDS, SOURCES, parse_proxy_list


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


if __name__ == '__main__':
    unittest.main()
