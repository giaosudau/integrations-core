# -*- coding: utf-8 -*-
# (C) Datadog, Inc. 2020-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import unicode_literals

import datetime
import decimal
import json
import logging
import time

import mmh3

try:
    import datadog_agent
except ImportError:
    from ....stubs import datadog_agent

# Unicode character "Arabic Decimal Separator" (U+066B) is a character which looks like an ascii
# comma, but is not treated like a comma when parsing metrics tags. This is used to replace
# commas so that tags which have commas in them (such as SQL queries) properly display.
import requests
from requests.adapters import HTTPAdapter, Retry

ARABIC_DECIMAL_SEPARATOR = '，'

LOGGER = logging.getLogger(__file__)


def compute_sql_signature(normalized_query):
    """
    Given an already obfuscated & normalized SQL query, generate its 64-bit hex signature.
    """
    if not normalized_query:
        return None
    # Note: please be cautious when changing this function as some features rely on this
    # hash matching the APM resource hash generated on our backend.
    return format(mmh3.hash64(normalized_query, signed=False)[0], 'x')


def normalize_query_tag(query):
    """
    Normalize the SQL query value to be used as a tag on metrics.

    HACK: This function substitutes ascii commas in the query with a special unicode
    character which is not normalized into a comma by metrics backend. This is a temporary
    hack to work around the bugs in the "Arbitrary Tag Values" feature on the backend
    which allows for any unicode string characters to be used as tag values without being
    escaped. Ascii commas in tag values are not currently supported in the query language,
    so this replacement is a workaround to display commas in tags but still allow metric
    queries to work.

    For Datadog employees, more background on "Arbitrary Tag Values":
    https://docs.google.com/document/d/1LQWw6ZiQZW18lknsBAZFMrba8BQ5yOmaWoEC7J1nLxU
    """
    query = query.replace(', ', '{} '.format(ARABIC_DECIMAL_SEPARATOR)).replace(',', ARABIC_DECIMAL_SEPARATOR)
    return query


def compute_exec_plan_signature(normalized_json_plan):
    """
    Given an already normalized json string query execution plan, generate its 64-bit hex signature.
    """
    if not normalized_json_plan:
        return None
    with_sorted_keys = json.dumps(json.loads(normalized_json_plan), sort_keys=True)
    return format(mmh3.hash64(with_sorted_keys, signed=False)[0], 'x')


class EventEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            return float(o)
        if isinstance(o, (datetime.date, datetime.datetime)):
            return o.isoformat()
        return super(EventEncoder, self).default(o)


def chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


# list of requests sessions and their endpoint urls [(http, url), ...]
_logs_endpoints = []


def _new_logs_session(api_key):
    http = requests.Session()
    http.mount("https://",
               HTTPAdapter(max_retries=Retry(connect=2, read=2, redirect=2, status=2, method_whitelist=['POST'])))
    http.headers.update({'DD-API-KEY': api_key})
    return http


def _logs_input_url(host):
    if host.endswith("."):
        host = host[:-1]
    if not host.startswith("https://"):
        host = "https://" + host
    return host + "/v1/input"


def _get_logs_endpoints():
    """
    Returns a list of requests sessions and their endpoint urls [(http, url), ...]
    Requests sessions are initialized the first time this is called and reused thereafter
    :return: list of (http, url)
    """
    global _logs_endpoints
    if _logs_endpoints:
        return _logs_endpoints

    # TODO: support other logs endpoint config options use_http, use_compression, compression_level

    url = _logs_input_url(datadog_agent.get_config('logs_config.dd_url') or "http-intake.logs.datadoghq.com")
    endpoints = [(_new_logs_session(datadog_agent.get_config('api_key')), url)]
    LOGGER.debug("initializing logs endpoint for sql exec plans. url=%s", url)

    for additional_endpoint in datadog_agent.get_config('logs_config.additional_endpoints') or []:
        api_key, host = additional_endpoint.get('api_key'), additional_endpoint.get('host')
        missing_keys = [k for k, v in [('api_key', api_key), ('host', host)] if not v]
        if missing_keys:
            LOGGER.warning("invalid endpoint found in logs_config.additional_endpoints. missing required keys %s",
                           ', '.join(missing_keys))
            continue
        url = _logs_input_url(host)
        endpoints.append((_new_logs_session(api_key), url))
        LOGGER.debug("initializing additional logs endpoint for sql exec plans. url=%s", url)

    _logs_endpoints = endpoints
    return _logs_endpoints


def submit_statement_sample_events(events, tags, source):
    """
    Submit the execution plan events to the event intake
    https://docs.datadoghq.com/api/v1/logs/#send-logs
    """
    ddtags = ','.join(tags)
    hostname = datadog_agent.get_hostname()
    service = next((t for t in tags if t.startswith('service:')), 'service:{}'.format(source))[len('service:'):]
    timestamp = time.time() * 1000

    def _to_log_event(e):
        return {
            'ddtags': ddtags,
            'hostname': hostname,
            'message': json.dumps(e, cls=EventEncoder),
            'service': service,
            'ddsource': source,
            'timestamp': timestamp
        }

    for http, url in _get_logs_endpoints():
        for chunk in chunks(events, 100):
            try:
                r = http.request('post', url,
                                 data=json.dumps([_to_log_event(e) for e in chunk]),
                                 timeout=5,
                                 headers={'Content-Type': 'application/json'})
                r.raise_for_status()
                LOGGER.debug("submitted %s exec plan events to %s", len(chunk), url)
            except requests.HTTPError as e:
                LOGGER.warning("failed to submit exec plan events to %s: %s", url, e)
            except Exception:
                LOGGER.exception("failed to submit exec plan events to %s", url)