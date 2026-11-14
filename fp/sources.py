'''Proxy source configuration and list-page parsing.

Each source is a plain dict, so adding a new site means appending one
entry to SOURCES -- no parsing or pool code changes required.
'''

import lxml.html as lh

# Fields extracted from every row, keyed by their column index in the
# source's table. All four current sites share the same table layout.
FIELDS = ('ip', 'port', 'country', 'anonymity', 'google', 'https')

_DEFAULT_COLUMNS = {'ip': 0, 'port': 1, 'country': 2,
                    'anonymity': 4, 'google': 5, 'https': 6}

# The four URLs mirror FreeProxy._website: the default page, the US and
# GB country pages, and the repeat (fallback) page. ProxyPool fetches all
# of them every round and applies country filtering after merging.
SOURCES = [
    {
        'id': 'sslproxies',
        'url': 'https://www.sslproxies.org',
        'table_xpath': '//*[@id="list"]//tr',
        'columns': dict(_DEFAULT_COLUMNS),
        'timeout': 10,
    },
    {
        'id': 'us-proxy',
        'url': 'https://www.us-proxy.org',
        'table_xpath': '//*[@id="list"]//tr',
        'columns': dict(_DEFAULT_COLUMNS),
        'timeout': 10,
    },
    {
        'id': 'uk-proxy',
        'url': 'https://free-proxy-list.net/uk-proxy.html',
        'table_xpath': '//*[@id="list"]//tr',
        'columns': dict(_DEFAULT_COLUMNS),
        'timeout': 10,
    },
    {
        'id': 'free-proxy-list',
        'url': 'https://free-proxy-list.net',
        'table_xpath': '//*[@id="list"]//tr',
        'columns': dict(_DEFAULT_COLUMNS),
        'timeout': 10,
    },
]


def parse_proxy_list(content, source):
    '''Parse one source's list page using its column mapping.

    Returns (rows, bad_rows). Rows with a column index out of range or a
    missing ip/port value are dropped and counted in bad_rows instead of
    failing the whole page. Each returned row maps every field in FIELDS
    to its stripped text.
    '''
    doc = lh.fromstring(content)
    tr_elements = doc.xpath(source['table_xpath'])
    columns = source['columns']
    rows = []
    bad_rows = 0
    # First <tr> is the table header.
    for tr in tr_elements[1:]:
        try:
            row = {field: tr[columns[field]].text_content().strip()
                   for field in FIELDS}
        except IndexError:
            bad_rows += 1
            continue
        if not row['ip'] or not row['port']:
            bad_rows += 1
            continue
        rows.append(row)
    return rows, bad_rows
