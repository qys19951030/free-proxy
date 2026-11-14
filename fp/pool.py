'''ProxyPool: harvest a batch of working proxies from all sources at once.

Unlike FreeProxy (one proxy per call, one site per round), ProxyPool
scrapes every configured source concurrently on each refresh, merges and
filters the candidates, checks them concurrently, and caches the results
on the pool instance so repeated get() calls stay cheap.
'''

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from fp.errors import FreeProxyException
from fp.fp import check_proxy_is_working
from fp.sources import SOURCES, parse_proxy_list

# Lower rank = stronger anonymity.
_ANONYMITY_RANK = {'elite': 0, 'anonymous': 1, 'transparent': 2}


def _normalize_anonymity(text):
    text = (text or '').strip().lower()
    if 'elite' in text:
        return 'elite'
    if text == 'anonymous':
        return 'anonymous'
    return 'transparent'


def _is_yes(text):
    return (text or '').strip().lower() == 'yes'


class ProxyPool:
    '''Collects working proxies in batches.

    Filter and check parameters mirror FreeProxy; the pool-specific ones:

    - max_concurrent: upper bound on concurrent proxy checks (default 20)
    - cache_ttl: seconds a scraped candidate list stays fresh (default 600)
    - cooldown: seconds a failed proxy is skipped before retry (default 300)
    - retries: extra attempts before a proxy is confirmed down (default 2)
    - sources: override the site list (defaults to fp.sources.SOURCES)
    '''

    def __init__(self, country_id=None, timeout=0.5, rand=False, anonym=False,
                 elite=False, google=None, https=False,
                 url='https://www.google.com', request_timeout=None,
                 max_concurrent=20, cache_ttl=600, cooldown=300, retries=2,
                 sources=None):
        if max_concurrent < 1:
            # 0 would park every check on the executor with no worker.
            raise ValueError('max_concurrent must be a positive integer')
        self.country_id = country_id
        self.timeout = timeout
        self.random = rand
        self.anonym = anonym
        self.elite = elite
        self.google = google
        self.schema = 'https' if https else 'http'
        self.url = url
        self.request_timeout = request_timeout
        self.max_concurrent = max_concurrent
        self.cache_ttl = cache_ttl
        self.cooldown = cooldown
        self.retries = retries
        self.sources = list(sources) if sources is not None else list(SOURCES)
        # Pool state shared across get() calls. One lock serializes get() so
        # concurrent callers share a single refresh round instead of each
        # scraping the sources on their own.
        self._lock = threading.Lock()
        self._cache = []
        self._cache_fetched_at = None
        self._checks = {}
        self._cooldowns = {}
        self._source_reports = []
        self._checked = 0
        self._cooldown_skipped = 0
        self._exhausted = False

    def get(self, n=1):
        '''Return up to n working proxies as a list of dicts.

        Stops checking as soon as n working proxies are found. If the pool
        runs out of candidates first, the partial list is returned and
        report()['exhausted'] is set. Raises FreeProxyException only when
        not a single working proxy was found.
        '''
        if n < 1:
            raise ValueError('n must be a positive integer')
        with self._lock:
            self._checked = 0
            self._refresh_if_stale()
            candidates = self._available_candidates()
            if self.random:
                random.shuffle(candidates)
            else:
                candidates.sort(key=self._candidate_sort_key)
            results = []
            to_check = []
            for entry in candidates:
                if len(results) >= n:
                    break
                check = self._checks.get(entry['address'])
                if check is not None and check['ok']:
                    # Verified earlier while this cache is fresh -- reuse
                    # instead of probing again.
                    results.append(self._record(entry, check))
                else:
                    to_check.append(entry)
            if len(results) < n and to_check:
                self._check_round(to_check, n - len(results), results)
            self._exhausted = len(results) < n
            if not results:
                raise FreeProxyException(self._no_proxies_message())
            if not self.random:
                results.sort(key=self._record_sort_key)
            return results[:n]

    def report(self):
        '''Stats from the latest refresh and get() round.'''
        return {
            'sources': [dict(source_report)
                        for source_report in self._source_reports],
            'checked': self._checked,
            'cooldown_skipped': self._cooldown_skipped,
            'exhausted': self._exhausted,
        }

    # -- fetching and merging ---------------------------------------------

    def _refresh_if_stale(self):
        if (self._cache_fetched_at is not None
                and time.monotonic() - self._cache_fetched_at < self.cache_ttl):
            return
        rows_by_source = {}
        reports = []
        with ThreadPoolExecutor(max_workers=max(1, len(self.sources))) as executor:
            futures = [executor.submit(self._fetch_source, source)
                       for source in self.sources]
            for future in futures:
                source_id, rows, source_report = future.result()
                rows_by_source[source_id] = rows
                reports.append(source_report)
        order = {source['id']: index for index, source in enumerate(self.sources)}
        reports.sort(key=lambda source_report: order[source_report['id']])
        self._source_reports = reports
        all_failed = bool(reports) and all(not r['ok'] for r in reports)
        if not (all_failed and self._cache):
            # A fully failed refresh keeps the previous cache rather than
            # dropping the last known candidates.
            self._cache = [entry for entry in self._merge(rows_by_source)
                           if self._matches(entry)]
            self._checks = {}
        self._cache_fetched_at = time.monotonic()

    def _fetch_source(self, source):
        source_report = {'id': source['id'], 'ok': False, 'error': None,
                         'elapsed_ms': None, 'candidates': 0, 'bad_rows': 0}
        rows = []
        start = time.monotonic()
        try:
            response = requests.get(source['url'],
                                    timeout=self._source_timeout(source))
            rows, bad_rows = parse_proxy_list(response.content, source)
            source_report['ok'] = True
            source_report['candidates'] = len(rows)
            source_report['bad_rows'] = bad_rows
        except requests.exceptions.RequestException:
            source_report['error'] = f"Request to {source['url']} failed"
        except Exception:
            source_report['error'] = +                f"Failed to parse proxy list from {source['url']}"
        source_report['elapsed_ms'] = (time.monotonic() - start) * 1000
        return source['id'], rows, source_report

    def _source_timeout(self, source):
        if self.request_timeout is not None:
            return self.request_timeout
        return source.get('timeout', 10)

    def _merge(self, rows_by_source):
        '''Merge per-source rows into one candidate per ip:port.'''
        merged = {}
        for source in self.sources:
            for row in rows_by_source.get(source['id'], []):
                address = f"{row['ip']}:{row['port']}"
                entry = merged.get(address)
                if entry is None:
                    entry = {'address': address, 'sources': [],
                             'anonymity': 'transparent', 'https': False,
                             'google': False, 'country': ''}
                    merged[address] = entry
                if source['id'] not in entry['sources']:
                    entry['sources'].append(source['id'])
                anonymity = _normalize_anonymity(row['anonymity'])
                if (_ANONYMITY_RANK[anonymity]
                        < _ANONYMITY_RANK[entry['anonymity']]):
                    entry['anonymity'] = anonymity
                entry['https'] = entry['https'] or _is_yes(row['https'])
                entry['google'] = entry['google'] or _is_yes(row['google'])
                if not entry['country']:
                    entry['country'] = row['country']
        return list(merged.values())

    def _matches(self, entry):
        '''Filter on merged annotations, mirroring FreeProxy._criteria.'''
        country_criteria = (True if not self.country_id
                            else entry['country'] in self.country_id)
        elite_criteria = (True if not self.elite
                          else 'elite' in entry['anonymity'])
        anonym_criteria = (True if (not self.anonym) or self.elite
                           else entry['anonymity'] == 'anonymous')
        google_criteria = (True if self.google is None
                           else self.google == entry['google'])
        https_criteria = True if self.schema == 'http' else entry['https']
        return (country_criteria and elite_criteria and anonym_criteria
                and google_criteria and https_criteria)

    # -- checking ----------------------------------------------------------

    def _available_candidates(self):
        now = time.monotonic()
        available = []
        skipped = 0
        for entry in self._cache:
            expiry = self._cooldowns.get(entry['address'])
            if expiry is not None and expiry > now:
                skipped += 1
                continue
            available.append(entry)
        self._cooldown_skipped = skipped
        return available

    def _check_round(self, entries, needed, results):
        '''Check entries concurrently, stopping early once needed successes
        have been collected.'''
        executor = ThreadPoolExecutor(max_workers=self.max_concurrent)
        futures = {executor.submit(self._check_one, entry['address']): entry
                   for entry in entries}
        successes = 0
        try:
            for future in as_completed(futures):
                if successes >= needed:
                    break
                entry = futures[future]
                ok, latency_ms, error = future.result()
                self._checked += 1
                if ok:
                    check = {'ok': True, 'latency_ms': latency_ms,
                             'checked_at': time.time(), 'error': None}
                    self._checks[entry['address']] = check
                    results.append(self._record(entry, check))
                    successes += 1
                else:
                    self._checks[entry['address']] = {
                        'ok': False, 'latency_ms': None,
                        'checked_at': time.time(), 'error': error}
                    self._cooldowns[entry['address']] = (
                        time.monotonic() + self.cooldown)
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False)

    def _check_one(self, address):
        '''Check one proxy, confirming failures before giving up.

        Returns (ok, latency_ms, error) where error is one of 'timeout',
        'connection_error', 'peer_mismatch'.
        '''
        error = None
        for _attempt in range(self.retries + 1):
            start = time.monotonic()
            try:
                working = check_proxy_is_working(
                    self.url, self.schema, self.timeout, address)
            except requests.exceptions.Timeout:
                error = 'timeout'
                continue
            except requests.exceptions.RequestException:
                error = 'connection_error'
                continue
            if working:
                return True, (time.monotonic() - start) * 1000, None
            # The request went through but the peer was not the proxy --
            # retrying cannot change that.
            return False, None, 'peer_mismatch'
        return False, None, error

    # -- ordering and records ----------------------------------------------

    @staticmethod
    def _candidate_sort_key(entry):
        return (_ANONYMITY_RANK[entry['anonymity']], not entry['https'],
                entry['address'])

    @staticmethod
    def _record_sort_key(record):
        latency_ms = record['latency_ms']
        return (_ANONYMITY_RANK[record['anonymity']], not record['https'],
                latency_ms is None, latency_ms or 0, record['proxy'])

    @staticmethod
    def _record(entry, check):
        return {
            'proxy': f"http://{entry['address']}",
            'sources': list(entry['sources']),
            'anonymity': entry['anonymity'],
            'https': entry['https'],
            'google': entry['google'],
            'country': entry['country'],
            'latency_ms': check['latency_ms'],
            'checked_at': check['checked_at'],
        }

    def _no_proxies_message(self):
        message = 'There are no working proxies at this time.'
        failures = [r for r in self._source_reports if not r['ok']]
        if (not self._cache and self._source_reports
                and len(failures) == len(self._source_reports)):
            details = '; '.join(f"{r['id']} ({r['error']})" for r in failures)
            message = f'{message} Failed sources: {details}'
        return message
