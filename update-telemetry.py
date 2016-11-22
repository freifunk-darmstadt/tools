#!/usr/bin/env python3
import psutil
import os
import json
import re
import itertools
from contextlib import contextmanager
import pprint
import time
import socket
import subprocess
import logging

logger = logging.getLogger(__name__)


def pairwise(iterable):
    "s -> (s0,s1), (s2,s3), (s4, s5), ..."
    a = iter(iterable)
    return zip(a, a)


@contextmanager
def get_socket(host, port):
    sock = socket.socket()
    sock.settimeout(1)
    sock.connect((host, port))
    yield sock
    sock.close()


@contextmanager
def get_unix_socket(filename):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1)
    sock.connect(filename)
    yield sock
    sock.close()


def write_to_graphite(data, prefix='freifunk', hostname=socket.gethostname()):
    if '.' in hostname:
        hostname = hostname.split('.')[0]
    now = time.time()
    with get_socket('stats.darmstadt.freifunk.net', 2013) as s:
        for key, value in data.items():
            line = "%s.%s.%s %s %s\n" % (prefix, hostname, key, value, now)
            s.sendall(line.encode('latin-1'))


def read_from_fastd_socket(filename):
    with get_unix_socket(filename) as client:
        try:
            strings = []
            while True:
                s = client.recv(8096)
                if not s:
                    break
                strings.append(s.decode('utf-8'))

            data = json.loads(''.join(strings))
            #pprint.pprint(data['statistics'])

            online_peers = len([None for name, d in data['peers'].items() if d['connection']])

            return {
                'peers.count': len(data['peers']),
                'peers.online': online_peers,
                'rx.packets': data['statistics']['rx']['packets'],
                'rx.bytes': data['statistics']['rx']['bytes'],
                'rx.reordered.bytes': data['statistics']['rx_reordered']['bytes'],
                'rx.reordered.packets': data['statistics']['rx_reordered']['packets'],
                'tx.bytes': data['statistics']['tx']['bytes'],
                'tx.packets': data['statistics']['tx']['packets'],
                'tx.dropped.bytes': data['statistics']['tx_dropped']['bytes'],
                'tx.dropped.packets': data['statistics']['tx_dropped']['packets'],
            }

        except Exception as e:
            print(e)
            return {}


def get_fastd_process_stats():
    for proc in psutil.process_iter():
        if proc.name() == 'fastd':
            # 11905: 00000000000000000000000001000000:0035 00000000000000000000000000000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 4469598 2 ffff880519be5100 0
            drop_count = 0
            for proto in ['udp', 'udp6']:
                with open('/proc/{}/net/{}'.format(proc.pid, proto), 'r') as fh:
                    for line in (line.strip() for line in fh.read().split('\n')):
                        if not line:
                            continue

                        if line.startswith('sl'):
                            continue

                        parts = line.split(' ')

                        drop_count += int(parts[-1])

            return drop_count

    return None


def get_neighbour_table_states(family=socket.AF_INET6):
    if family is socket.AF_INET:
        family = '-4'
    elif family is socket.AF_INET6:
        family = '-6'
    else:
        return
    response = subprocess.check_output(
        ['/bin/ip', family, 'neigh', 'show', 'nud', 'all']
    ).decode()

    states = {'PERMANENT': 0, 'NOARP': 0, 'REACHABLE': 0, 'STALE': 0, 'NONE': 0,
              'INCOMPLETE': 0, 'DELAY': 0, 'PROBE': 0, 'FAILED': 0}

    for neigh_entry in response.split('\n'):
        if not neigh_entry:
            continue
        states[neigh_entry.split()[-1]] += 1

    return states


def main():
    fastd_sockets = (
        ('0', '/run/fastd-ffda-vpn.sock'),
        ('1', '/run/fastd-ffda-vpn1.sock'),
    )

    device_name_mapping = {
        'freifunk': 'ffda-br',
        'bat0': 'ffda-bat',
        'mesh-vpn': 'ffda-vpn'
    }
    device_whitelist = [
        'eth0',
        'tun-ffrl-ber',
        'tun-ffrl-dus',
        'tun-ffrl-fra',
        'tun-ffda-gw01',
        'tun-ffda-gw02',
        'tun-ffda-gw03',
        'tun-ffda-gw04',
        'ffda-vpn',
        'ffda-bat',
        'ffda-br',
        'icvpn',
        'ffda-transport',
        'services',
    ]

    fields = [
        'bytes', 'packets', 'errs', 'drop', 'fifo',
        'frame', 'compressed', 'multicast',
    ]
    field_format = '(?P<{direction}_{field}>\d+)'

    pattern = re.compile(
        '^\s*(?P<device_name>[\w-]+):\s+' + '\s+'.join(
            itertools.chain.from_iterable((field_format.format(direction=direction, field=field)
                                           for field in fields) for direction in ['rx', 'tx'])
        )
    )

    update = {}
    with open('/proc/net/dev') as fh:
        lines = fh.readlines()
        for line in lines:
            m = pattern.match(line)
            if m:
                groupdict = m.groupdict()
                device_name = groupdict.pop('device_name')
                device_name = device_name_mapping.get(device_name, device_name)
                if device_name in device_whitelist or device_name.endswith('-vpn') or \
                        device_name.endswith('-bat') or \
                        device_name.endswith('-br') or \
                        device_name.endswith('-transport'):
                    for key, value in groupdict.items():
                        direction, metric = key.split('_')
                        update['%s.%s.%s' % (device_name, direction, metric)] = value

    with open('/proc/loadavg', 'r') as fh:
        line = fh.read()
        values = line.split(' ', 3)
        update['load.15'] = values[0]
        update['load.5'] = values[1]
        update['load.1'] = values[2]

    for key in ['count', 'max']:
        try:
            with open('/proc/sys/net/netfilter/nf_conntrack_%s' % key, 'r') as fh:
                update['netfilter.%s' % key] = fh.read().strip()
        except IOError as e:
            pass

    with open('/proc/net/snmp6', 'r') as fh:
        for line in fh.readlines():
            key, value = line.split(' ', 1)
            value = value.strip()
            update['ipv6.%s' % key] = value

    with open('/proc/net/snmp', 'r') as fh:
        for heading, values in pairwise(fh.readlines()):
            section, headings = heading.split(':')
            headings = headings.strip().split(' ')
            _, values = values.split(':')
            values = values.strip().split(' ')
            for key, value in zip(headings, values):
                update['ipv4.%s.%s' % (section, key)] = value

    for af, prefix in [(socket.AF_INET, 'ipv4.Neigh'),
                       (socket.AF_INET6, 'ipv6.Neigh')]:
        for state, count in get_neighbour_table_states(af).items():
            update['{0}.{1}'.format(prefix, state.lower())] = count

    with open('/proc/stat', 'r') as fh:
        for line in fh.readlines():
            key, value = line.split(' ', 1)
            if key == 'ctxt':
                update['context_switches'] = value.strip()
                break

    for name, filename in fastd_sockets:
        if not os.path.exists(filename):
            continue

        data = read_from_fastd_socket(filename)
        if len(data) > 0:
            update.update({'fastd.%s.%s' % (name, key): value for (key, value) in data.items()})

    fastd_drops = get_fastd_process_stats()
    if fastd_drops:
        update['fastd.drops'] = fastd_drops

    #pprint.pprint(update)
    write_to_graphite(update)

if __name__ == "__main__":
    main()
