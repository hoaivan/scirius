from __future__ import print_function, division
import json
import logging
import socket
import urllib2
import ssl
import models
import tables
import django_tables2
import es_backend
import time
import itertools
import operator

import functools
from scirius.utils import parallel_map

from django.conf import settings

# Create a context to ignore ssl errors
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

TIMEOUT = 30
humio_logger = logging.getLogger('humio')

HUMIO_TIMELINE_HOST = 'suricata'

ALERTS_FILTER = '"event_type" = "alert"'
#ALERTS_FILTER = ''

#HUMIO_REPLACE_SURICATA_HOSTNAME = True
HUMIO_REPLACE_SURICATA_HOSTNAME = False
HUMIO_LOG_SURICATA_HOSTNAME = 'taro'

HUMIO_ENDPOINT_REPO_QUERY = "/api/v1/repositories/%s/query"

HUMIO_DEFAULT_SORT_LIMIT = 20000

FIELD_REPLACEMENTS = {
    'alert.category.raw': 'alert.category',
    'alert.signature_id': 'alert.signature_id',
    'src_ip': 'src_ip',
    'alert.source.ip': 'src_ip',
    'dest_ip': 'dest_ip',
    'alert.target.ip': 'dest_ip',
}


def _build_query(filters, hosts=None):
    """Build a humio query from a list of filters.

    Filters will be applied according to their order in the filters list.

    :param filters: list of filters (strings)
    :param hosts: list of hosts that we want the query to target
    :return: 
    """

    filters = filter(lambda f: f is not None, filters)
    filter_query = '|'.join(filters)
    if hosts:
        # FIXME: Testing only
        # Replace occurences of 'suricata' with HUMIO_LOG_SURICATA_HOSTNAME
        if HUMIO_REPLACE_SURICATA_HOSTNAME:
            hosts = [HUMIO_LOG_SURICATA_HOSTNAME if h == 'suricata' else h for h in hosts]

        hosts_query = ' or '.join(map(lambda h: 'host = "%s"' % h.replace('\\', '\\\\').replace('"', '\\"'), hosts))
        return hosts_query + '|' + filter_query
    else:
        return filter_query


def _fix_qfilter(qfilter):
    """Fixes elasticsearch based filters to work as humio filters.

    Apply replacements on qfilter to convert filters from the elasticsearch query language to the humio query language.

    :param qfilter:
    :return: updated qfilter
    """

    if qfilter:
        split = qfilter.split(':', 1)
        print('SPLIT', split)
        if FIELD_REPLACEMENTS.has_key(split[0]):
            split[0] = FIELD_REPLACEMENTS[split[0]]
            qfilter = '='.join(split)
    return qfilter


def _fix_sorting(sort_key, sort_order, sort_key_field_map, limit=HUMIO_DEFAULT_SORT_LIMIT,
               default_sort_key=None, default_sort_order='desc'):
    """Fixes elasticsearch based sorting to work as humio filters

    Map table columns to the humio query field to sort by and a list of humio fields
    to add to a groupBy statement.

    NOTE: Default sort order is used when no sort key is specified.

    :param sort_key_field_map: A dictionary mapping column keys (eg. 'hits' and 'category')
                               to a dict {'groupby_fields' <list of fields>, 'field': <humio field>}
    :return: tuple: (<list of groupby fields>, <sort_filter>)
    """
    if not sort_key:
        if not default_sort_key:
            humio_logger.error('_fix_sorting: sort key was None and default_sort_key was None')
            return None
        sort_key = default_sort_key
        sort_order = default_sort_order

    if sort_key not in sort_key_field_map.keys():
        humio_logger.error('_fix_sorting: unexpected sort key: %s, not in field_map' % sort_key)
        return None

    sort_order = sort_order or default_sort_order

    map_entry = sort_key_field_map[sort_key]
    groupby_fields, sort_field = map_entry['groupby_fields'], map_entry['field']

    sort_filter = 'sort(field=[%s], limit=%d, order=%s)' % (sort_field, int(limit), sort_order)

    return groupby_fields, sort_filter

def _urlopen(request):
    try:
        out = urllib2.urlopen(request, timeout=settings.HUMIO_TIMEOUT, context=ctx)
    except (urllib2.URLError, socket.timeout) as e:
        msg = unicode(e)
        humio_logger.exception(msg)
        raise RuntimeError(msg)
    return out


class HumioClient(es_backend.ESBackend):
    def __init__(self):
        es_backend.ESBackend.__init__(self)
        self._api_token = settings.HUMIO_API_TOKEN
        self._host = settings.HUMIO_HOST
        self._repository = settings.HUMIO_REPOSITORY

    def _humio_request(self, endpoint, query_data):
        headers = {
            'content-type': 'application/json',
            'accept': 'application/json',
            'Authorization': 'Bearer ' + self._api_token
        }
        url = self._host + endpoint
        req = urllib2.Request(url, query_data, headers=headers)
        res = _urlopen(req)
        data = res.read()
        return data

    def _humio_query(self, filters, start=0, end=None, is_live=False, fix_filters=True, hosts=None):
        """ Does a humio query with the given parameters.

        :param start: time since epoch in milliseconds
        :param end: time since epoch in milliseconds, string or None
        """
        if fix_filters:
            filters = list(map(_fix_qfilter, filters))

        if not end:
            end = 'now'

        query = {
            'queryString': _build_query(filters, hosts=hosts),
            'start': int(start),
            'end': end,
            'isLive': is_live
        }

        print('QUERYTSTRING', query['queryString'])

        query_data = json.dumps(query)
        api_endpoint = HUMIO_ENDPOINT_REPO_QUERY % self._repository
        res = self._humio_request(api_endpoint, query_data)
        return json.loads(res)

    def _create_custom_field_names_filter(self, from_to_dict, drop_old=True):
        """Create a humio filter to convert output keys to the ones specified in from_to_dict.

        :param drop_old: drop 'from' keys
        """
        drop_filter = ''
        if drop_old:
            drop_filter = '| drop([%s])' % (','.join(from_to_dict.keys()))

        return '|'.join(['%s := %s' % (v, k) for k, v in from_to_dict.items()]) + drop_filter


    def _get_rule_stats(self, count, from_date, qfilter=None,
                        extra_fields=None, extra_filters=[], hosts=None,
                        sort_key=None, sort_order=None):
        """Get the top <count> rules sorted by key <sort_key> and ordered by <sort_order>"""

        if not extra_fields:
            extra_fields = []

        RULE_STATS_SORTING_FIELD_MAP = {
            'hits':      {'groupby_fields': [],                   'field': '_count'},
            'sid':       {'groupby_fields': [],                   'field': 'alert.signature_id'},
            'msg':       {'groupby_fields': ['alert.signature'],  'field': 'alert.signature'},
            'category':  {'groupby_fields': ['alert.category'],   'field': 'alert.category'},
        }

        groupby_fields, sort_filter = _fix_sorting(sort_key, sort_order,
                                                   RULE_STATS_SORTING_FIELD_MAP, limit=count,
                                                   default_sort_key='hits')
        print('GET_RULE_STATS_SORT_FILTER', sort_filter)

        fields = extra_fields + ['alert.signature_id'] + groupby_fields

        fields_str = 'field=[' + ','.join(fields) + ']'
        # FIXME: It is not necessary to count alerts if not sorting by key 'hits'
        query_str = 'groupBy(%s, function=count())' % fields_str

        return self._humio_query(filters=[ALERTS_FILTER, qfilter, query_str, sort_filter] + extra_filters,
                                 start=from_date, hosts=hosts)

    def get_rules_stats_table(self, request, hosts=None,
                              count=20, from_date=0, qfilter=None,
                              sort_order=None, sort_key=None):
        dict_data = self._get_rule_stats(count, from_date, qfilter, hosts=hosts,
                                           sort_key=sort_key, sort_order=sort_order)

        def rule_from_entry(entry):
            sid = int(entry['alert.signature_id'])
            try:
                #print('GETTING ENTRY', entry)
                rule = models.Rule.objects.get(sid=sid)
                rule.hits = int(entry['_count'])
            except:
                print('Can not find rule with sid {}'.format(sid))
                return None
            return rule

        rules = [rule_from_entry(entry) for entry in dict_data]
        rules_table = tables.ExtendedRuleTable(rules)
        django_tables2.RequestConfig(request).configure(rules_table)
        return rules_table

    def get_rules_stats_dict(self, request, hosts=None,
                             count=20, from_date=0, qfilter=None,
                             sort_order=None, sort_key=None):
        field_names_filter =\
            self._create_custom_field_names_filter({'alert.signature_id': 'key',
                                                    '_count': 'doc_count'})

        return self._get_rule_stats(count, from_date, extra_filters=[field_names_filter], hosts=hosts,
                               sort_key=sort_key, sort_order=sort_order)

    def get_field_stats_table(self, request, field, FieldTable,
                              hosts=None, key='host', count=20,
                              from_date=0, qfilter=None, raw=False):
        data = self.get_field_stats_dict(request, field, hosts=hosts, key=key, count=count,
                                         from_date=from_date, qfilter=qfilter)
        if data is None:
            objects = FieldTable([])
            django_tables2.RequestConfig(request).configure(objects)
            return objects
        objects = []
        for elt in data:
            fstat = {key: elt['key'], 'count': int(elt['doc_count'])}
            objects.append(fstat)
        objects = FieldTable(objects)
        django_tables2.RequestConfig(request).configure(objects)
        return objects

    def get_field_stats_dict(self, request, field, hosts=None, key='host',
                             count=20, from_date=0, qfilter=None, raw=False):
        if field in FIELD_REPLACEMENTS:
            field = FIELD_REPLACEMENTS[field]
        if field:
            field_names_filter = self._create_custom_field_names_filter({
                field: 'key',
                '_count': 'doc_count'})
        else:
            field_names_filter = self._create_custom_field_names_filter({
                'alert.signature_id': 'key',
                '_count': 'doc_count'})


        return self._es_get_field_stats_json(request, field, hosts=hosts, key=key, count=count,
                                                  from_date=from_date, qfilter=qfilter, filters=[field_names_filter])

    def _es_get_field_stats_json(self, request, field, hosts=None, key='host', count=20,
                                 from_date=0, qfilter=None, raw=False, filters=[]):
        query_str = 'groupBy(field=%s, function=count(), limit=%d)' % (field, count)
        return self._humio_query(filters=[ALERTS_FILTER, qfilter, query_str] + filters, start=from_date, hosts=hosts)

    def get_alerts_count(self, from_date=0, hosts=None, qfilter=None, prev=0):
        """Gets the previous alert count and the current alert count.

        :return: {'prev_doc_count': <previous>, 'doc_count' <current>}
        """

        # FIXME: Currently does two queries, one for doc_count and one for prev_doc_count.
        from_date_ms = int(int(from_date)//1000)*1000
        current_time_ms = int(time.time())*1000
        diff_ms = (current_time_ms - from_date_ms)
        prev_start_ms = (from_date_ms - diff_ms)
        query_str = 'count()'

        filters=[ALERTS_FILTER, qfilter, query_str]
        data = self._humio_query(filters=filters, start=from_date_ms, hosts=hosts)
        cur_count = data[0]['_count']

        if prev:
            prev_data = self._humio_query(filters=filters, start=prev_start_ms, end=from_date_ms, hosts=hosts)
            return {'doc_count': int(cur_count), 'prev_doc_count': int(prev_data[0]['_count'])}

        return {'doc_count': cur_count}

    def get_timeline(self, from_date=0, interval=None, hosts=None, qfilter=None, tags=False):
        if not hosts:
            hosts = []
        n_queries = len(hosts)/10

        def parallel_query(from_date, interval, hosts, qfilter, tags, buckets=None):
            chunk_size = 10
            n_chunks = len(hosts)//chunk_size
            chunks = [hosts[s:s+chunk_size] for s in range(0, len(hosts), chunk_size)]

            end_date = int(time.time()) * 1000

            common_kwargs = {'from_date': from_date, 'interval': interval,
                             'qfilter': qfilter, 'tags': tags, 'end_date': end_date,
                             'buckets': buckets}

            wrapper = lambda i, h, **kwargs: i.get_timeline_sp(hosts=h, **kwargs)
            f = functools.partial(wrapper, self, **common_kwargs)
            mergedict = lambda x,y: (x if x.update(y) else x)
            result = functools.reduce(mergedict, parallel_map(f, chunks))
            return result

        if n_queries > 2:
            results = parallel_query(from_date, interval, hosts, qfilter, tags, buckets=100/(2*n_queries))
            #results = parallel_query(from_date, interval, hosts, qfilter, tags)
        else:
            results = self.get_timeline_sp(from_date, interval, hosts, qfilter, tags, buckets=100/(2*n_queries))
        return results

    def get_timeline_sp(self, from_date=0, interval=None, hosts=None, qfilter=None, tags=False, end_date=None, buckets=None):
        """Gets a list of alert counts at a given interval from the given from date.
        :param buckets: the amount of alert count points per host to have on the timeline
        :return dict on the form:
        {
         'from_date': <from_date>,
         'interval': <interval>,
          <host>: {entries: [{time: <bucket>, count: <count>}]},
          ...
        }
        :
        """
        if not buckets:
            buckets = 100

        from_date = int(from_date)

        if not interval:
            interval = int((time.time() - (int(from_date) / 1000)) / buckets) * 1000

        query_str = 'bucket(field=[host], function=count(), span=%sms) | sort(host, limit=%d)' % (interval, HUMIO_DEFAULT_SORT_LIMIT)

        data = self._humio_query(filters=[ALERTS_FILTER, qfilter, query_str], start=from_date, hosts=hosts, end=end_date)

        if HUMIO_REPLACE_SURICATA_HOSTNAME:
            key_function = lambda x: HUMIO_TIMELINE_HOST
        else:
            key_function = operator.itemgetter('host')

        # transform from the format:
        # [{host: <host>, _count: <count>, _bucket: <bucket>}, ...]
        # to {<host>: {entries: {time: <bucket>, count: <count>}}
        rdata = {key: {'entries': [{
            'time': int(v['_bucket']),
            'count': int(v['_count'])
        } for v in values]}
            for key, values in itertools.groupby(data, key=key_function)}

        rdata['from_date'] = int(from_date)
        rdata['interval'] = int(interval)
        return rdata

    def get_rules_per_category(self, from_date=0, hosts=None, qfilter=None):
        """Gets a list of alerted rules grouped into categories from humio.

        :param from_date:
        :param hosts:
        :param qfilter:
        :return dict on the form:
            {'key': 'categories',
                 'children': [
                    {'doc_count': <alert count for rules in this category>,
                      'children': [
                        {'key': <sid>, 'msg': <rule msg>, 'doc_count': <rule alert count>},
                        {'key': <sid>, 'msg': <rule msg>, 'doc_count': <rule alert count>},
                          ...
                      ],
                    },
                    {'doc_count': <alert count for rules in this category>,
                      'children': [
                        {'key': <sid>, 'msg': <rule msg>, 'doc_count': <rule alert count>},
                        {'key': <sid>, 'msg': <rule msg>, 'doc_count': <rule alert count>},
                          ...
                      ],
                    },
                    ...
                 ]
             }
                    :
        """

        # This query groups alert signatures, counts them, and then
        # groups the result by category, collecting the alerts into
        # a list-like result.
        # This query will be changed to one utilizing sub-queries
        # when these become available in humio.

        query_str = """
        select([alert.signature_id, alert.category, alert.signature])
        | groupBy(
                [alert.signature_id, alert.category, alert.signature],
                function=count(as="doc_count"))
        | msg := alert.signature | key := alert.signature_id
        | drop([alert.signature, alert.signature_id])
        | groupBy(
                alert.category,
                function=[
                        count(as="total_count"),
                        collect([key, msg, doc_count], multival=false)])
        | drop(@rawstring)
        """

        cdata = self._humio_query(filters=[ALERTS_FILTER, qfilter, query_str], start=from_date, hosts=hosts)

        def get_children_from_entry(e):
            children = [{} for _ in range(int(e['total_count']))]
            for index, field, value in map(lambda (i, f, v): (int(i), f, v),
                                       map(lambda (k, v): (k[8:k.index(']')], k[k.index(']') + 2:], v),
                                       filter(lambda (k, v): k.startswith('_events'), e.items()))):
                children[index][field] = value
            return children

        def sum_total(entry):
            entry['doc_count'] = sum(int(e['doc_count']) for e in entry['children'])
            return entry

        rdata = {
            'key': 'categories',
            'children': list(map(sum_total, [
                {
                    'key': entry['alert.category'],
                    'children': get_children_from_entry(entry)
                }
                for entry in cdata
            ]))
        }

        return rdata

    def get_sid_by_hosts(self, request, sid, count=20, from_date=0, dict_format=False,
                         sort_key=None, sort_order=None):
        args = [request, sid]
        kwargs = {'count': count, 'from_date': from_date, 'sort_key': sort_key, 'sort_order': sort_order}
        if dict_format:
            return self.get_sid_by_hosts_dict(*args, **kwargs)
        return self.get_sid_by_hosts_table(*args, **kwargs)

    def get_sid_by_hosts_dict(self, request, sid, count=20, from_date=0,
                              sort_key=None, sort_order=None):
        """
        :return dict on the format {"rule":[{"key":"<host>","doc_count":<alerts on rule with given sid>}]}:
        """

        HITS_BY_HOSTS_SORTING_FIELD_MAP = {
            'host':  {'groupby_fields': ['host'], 'field': 'host'},
            'count': {'groupby_fields': ['host'], 'field': '_count'},
        }

        groupby_fields, sort_filter = _fix_sorting(sort_key, sort_order,
                                                   HITS_BY_HOSTS_SORTING_FIELD_MAP, limit=count,
                                                   default_sort_key='count')

        groupby_field = 'host'
        query_str = 'alert.signature_id = %s | groupBy(%s, function=count())' % (sid, groupby_field)
        data = self._humio_query(filters=[ALERTS_FILTER, query_str, sort_filter], start=from_date)

        # transform from the format:
        # [{host: <host>, _count: <count>}, ...]
        # to {'rule': ['key': <host>, 'doc_count': <count>}, ...]}
        rdata = {'rule': []}
        for b in data:
            entry = {'key': b['host'], 'doc_count': int(b['_count'])}
            rdata['rule'].append(entry)
        return rdata

    def get_sid_by_hosts_table(self, request, sid, count=20, from_date=0, sort_key=None, sort_order=None):
        data = self.get_sid_by_hosts_dict(request, sid, count, from_date, sort_key, sort_order)
        rdata = [{'host': e['key'], 'count': e['doc_count']} for e in data['rule']]
        stats = tables.RuleStatsTable(rdata)
        django_tables2.RequestConfig(request).configure(stats)
        return stats

    def get_es_major_version(self):
        return settings.HUMIO_SPOOF_ES_VERSION

    # NOTE: There are several es_* functions that are not implemented.
