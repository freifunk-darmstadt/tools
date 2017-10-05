#!/usr/bin/env python
import os
import logging
import pprint
import socket
import time
import pytz
import datetime
import dateutil.parser
from contextlib import contextmanager

import requests

logger = logging.getLogger(__name__)


@contextmanager
def get_socket(host, port):
    sock = socket.socket()
    sock.settimeout(3)
    sock.connect((host, port))
    yield sock
    sock.close()


def write_to_graphite(data, prefix='freifunk', log=None):
    # {u'status': u'up', u'graph': {u'max': 539, u'uptime': 90262, u'total': 9435, u'connected': 297, u'cap': 3000}, u'timestamp': 1421072166316}
    now = time.time()
    with get_socket(os.getenv('GRAPHITE_HOST', 'localhost'), 2013) as s:
        for key, value in data.items():
            line = "%s.%s %s %s\n" % (prefix, key, value, now)
            #            if not log is None:
            #                if 'andi-' in key:
            #                    log.debug(line)
            s.sendall(line.encode('latin-1'))

def parse_graph(nodes):
    # parse graph
    URL = 'https://meshviewer.darmstadt.freifunk.net/data/ffda/graph.json'
    update = {}

    data = requests.get(URL, timeout=1).json()

    links = data.get('batadv', {}).get('links', [])
    graph_nodes = data.get('batadv', {}).get('nodes', [])

    del data

    edges = {}

    for link in links:
        key = '{}.{}'.format(min(link['source'], link['target']), max(link['source'], link['target']))
        if not key in edges:
            edges[key] = link

    del links

    deletes = []
    for key, edge in edges.items():
        try:
            source_id = graph_nodes[edge['source']]['node_id']
            target_id = graph_nodes[edge['target']]['node_id']
        except KeyError:
            deletes.append(key)
        else:
            try:
                edge['source'] = nodes[source_id]
                edge['target'] = nodes[target_id]
            except KeyError:
                pass


    for d in deletes:
        del edges[d]

    values = {}

    for key, edge in edges.items():
        try:
            key = 'link.{}.{}.tq'.format(edge['source']['nodeinfo']['hostname'],edge['target']['nodeinfo']['hostname'])
        except TypeError:
            pass
        else:
            values[key] = 1.0/edge['tq']

    return values

def yield_nodes(data):
    version = int(data.get('version', 0))
    if version == 2:
        for node in data['nodes']:
            yield node
        return
    elif version == 1:
        for mac, node in data['nodes'].items():
            yield node
        return
    elif version == 0:
        for mac, node in data.items():
            yield node
    else:
        raise RuntimeError("Invalid version: %i" % version)

def main():
    logging.basicConfig(level=logging.DEBUG)
    offline_threshold = datetime.timedelta(seconds=600)
    while True:
        now = datetime.datetime.utcnow().replace(tzinfo=pytz.UTC)
        pprinter = pprint.PrettyPrinter(indent=4)

        URL = 'https://meshviewer.darmstadt.freifunk.net/data/ffda/nodes.json'

        gateways = []

        try:
            client_count = 0

            r = requests.get(URL, timeout=1)
            print(r.headers)
            data = r.json()
            known_nodes = 0
            online_nodes = 0
            update = {} # parse_graph(nodes)
            gateway_count = 0
            for node in yield_nodes(data):
                known_nodes += 1
                try:
                    hostname = node['nodeinfo']['hostname']
                    if hostname.startswith('gw'):
                        hostname = hostname.split('.', 1)[0]

                    if 'flags' in node:
                        flags = node['flags']
                        if flags['online']:
                            online_nodes += 1

                        if flags.get('gateway', False):
                            gateway_count += 1
                            gateways.append(hostname)

                    else:
                        if 'lastupdate' in node:
                            delta = now - dateutil.parser.parse(node['lastupdate']['statistics'])
                            if delta < offline_threshold:
                                online_nodes += 1

                    statistics = node['statistics']
                    # try:
                    #  loadavg = statistics['loadavg']
                    #  update['%s.loadavg' % hostname] = loadavg
                    # except KeyError:
                    #  pass
                    # try:
                    #  uptime = statistics['uptime']
                    #  update['%s.uptime' % hostname] = uptime
                    # except KeyError:
                    #  pass

                    try:
                        clients = statistics['clients']
                        if type(clients) is dict:
                            client_count += clients['total']
                            update['%s.clients' %hostname] = clients['total']
                            update['%s.clients.wifi5' %hostname] = clients.get('wifi5', 0)
                            update['%s.clients.wifi24' %hostname] = clients.get('wifi24', 0)
                        else:
                            client_count += int(clients)
                            update['%s.clients' % hostname] = int(clients)
                    except KeyError:
                        pass

                    try:
                        traffic = statistics['traffic']
                        for key in ['tx', 'rx', 'mgmt_tx', 'mgmt_rx', 'forward']:
                            if len(traffic[key]) == 0:
                                continue
                            update['%s.traffic.%s.packets' % (hostname, key)] = traffic[key]['packets']
                            update['%s.traffic.%s.bytes' % (hostname, key)] = traffic[key]['bytes']
                    except KeyError as e:
                        print('failed to get traffic:', e, key, traffic)

                    try:
                        key = 'firmware.release.%s' % node['nodeinfo']['software']['firmware']['release']
                        if key not in update:
                            update[key] = 0
                        update[key] += 1
                    except KeyError:
                        pass

                    try:
                        key = 'firmware.base.%s' % node['nodeinfo']['software']['firmware']['base']
                        if key not in update:
                            update[key] = 0
                        update[key] += 1
                    except KeyError:
                        pass

                    if 'memory' in statistics:
                        memory = statistics['memory']
                        print(memory)
                        if type(memory) is dict:
                            update['%s.memory_usage' % hostname] = (float(memory['total']) -float(memory['free']))/float(memory['total'])

                    for key in ['memory_usage', 'rootfs_usage', 'uptime', 'loadavg']:
                        try:
                            val = statistics[key]
                            update['%s.%s' % (hostname, key)] = val
                        except KeyError:
                            pass
                except KeyError as e:
                    print(time.time())
                    print('error while reading ', node, e)
                    print(e)

                #            print(time.time())
            update['clients'] = client_count
            update['known_nodes'] = known_nodes
            update['online_nodes'] = online_nodes
            update['gateways'] = gateway_count
            #            print(client_count)
            #pprint.pprint(update)
            write_to_graphite(update, log=logger)
        except Exception as e:
            print(e)

        time.sleep(25)


if __name__ == "__main__":
    main()
