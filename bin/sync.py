#!/usr/bin/env python3
import logging
import consulate
from settings import settings
from snakebite.client import Client
import socket
import json
import os
import time
import random
import sys
import string

class Segment(object):
    def __init__(self, consul, segment_id, size, registry):
        self.consul = consul
        self.id = segment_id
        self.size = size
        self.registry = registry
    def all_copies(self, full_record=False):
        ''' returns the 'assigned' SegmentCopies, whether or not they are 'up' '''
        assignments = []
        for host in self.registry.get_hosts(type='read'):
            # then for each host, we'll check the k/v store
            record = None
            if full_record:
                record = self.consul.kv.get_record(self.host_key(host['Node']))
            elif self.consul.kv.get(self.host_key(host['Node'])):
                record = self.host_key(host['Node'])
                assignments.append(self.host_key(host['Node']))
            if record:
                assignments.append(record)
        return assignments
    def up_copies(self):
        '''returns the 'up' SegmentCopies'''
        return self.consul.catalog.service("trough/read/%s" % self.id)
    def host_key(self, host):
        return "%s/%s" % (host, self.id)
    def is_assigned_to_host(self, host):
        return bool(self.consul.kv.get(self.host_key(host)))
    def minimum_assignments(self):
        '''This function should return the minimum number of assignments which is acceptable for a given segment.'''
        return 2
        raise Exception('Not Implemented')

class HostRegistry(object):
    ''' this should probably implement all of the 'host' oriented functions below. '''
    def __init__(self, consul):
        self.consul = consul
    def get_hosts(self, type='read'):
        return self.consul.catalog.service('trough-%s-nodes' % type)
    def look_for_hosts(self):
        output = bool(self.get_hosts(type='read') + self.get_hosts(type='write'))
        logging.debug("Looking for hosts. Found: %s" % output)
        return output
    def host_load(self):
        output = []
        for host in self.get_hosts(type='read'):
            assigned_bytes = sum(self.consul.kv.get("%s/" % host['Node'], [0]))
            total_bytes = self.consul.kv.get("%s" % host['Node'])
            total_bytes = 0 if total_bytes in ['null', None] else int(total_bytes)
            output.append({
                'Node': host['Node'],
                'remaining_bytes': total_bytes - assigned_bytes,
                'assigned_bytes': assigned_bytes,
                'total_bytes': total_bytes,
                'load_ratio': (total_bytes - assigned_bytes) / (total_bytes if total_bytes > 0 else 1)
            })
        return output
    def host_bytes_remaining(self):
        output = self.host_load()
        return 
    def underloaded_hosts(self):
        output = []
        hosts = self.host_load()
        average_load_ratio = sum([host['load_ratio'] for host in hosts]) / len(hosts)
        # 5% below the average load is an acceptable ratio
        for host in hosts:
            # TODO: figure out a better way to describe the "acceptably empty" percentage.
            # Noah suggests (a multiplication factor) * (the largest segment / total dataset size), capped at 1.0 (100%)
            if host['load_ratio'] < (average_load_ratio - 0.05):
                host['average_load_ratio'] = average_load_ratio
                output.append(host)
        return output
    def host_is_advertised(self, host):
        logging.info('Checking if "%s" is advertised.' % host)
        for advertised_host in self.get_hosts(type='read'):
            if advertised_host['Node'] == host:
                logging.info('Found that "%s" is advertised.' % host)
                return True
        return False
    def advertise(self, name, service_id, address=settings['EXTERNAL_IP'], \
            port=settings['READ_PORT'], tags=[], ttl=str(settings['READ_NODE_DNS_TTL']) + 's'):
        logging.info('Advertising: name[%s] service_id[%s] at /v1/catalog/service/%s on IP %s:%s with TTL %ss' % (name, service_id, service_id, address, port, ttl))
        self.consul.agent.service.register(name, service_id=service_id, address=address, port=port, tags=tags, ttl=ttl)
    def health_check(self, pool, service_name):
        return self.consul.health.node("trough/%s/%s" % (pool, service_name))
    def create_health_check(self, name, pool, service_name, ttl, notes):
        return self.consul.agent.check.register(name, check_id="service:trough/%s/%s" % (pool, service_name), ttl=str(ttl)+"s", notes=notes)
    def reset_health_check(self, pool, service_name):
        logging.warn('Updating health check for pool: "%s", service_name: "%s".' % (pool, service_name))
        return self.consul.agent.check.ttl_pass("service:trough/%s/%s" % (pool, service_name))
    def assign(self, host, segment):
        logging.info("Assigning segment: %s to '%s'" % (segment.id, host['Node']))
        logging.info('Setting key "%s" to "%s"' % (segment.host_key(host['Node']), segment.size))
        self.consul.kv[segment.host_key(host['Node'])] = segment.size
    def unassign(self, host, segment):
        logging.info("Unassigning segment: %s on '%s'" % (segment, host))
        del self.consul.kv[segment.host_key(host)]
    def set_quota(self, host, quota):
        logging.info('Setting quota for host "%s": %s bytes.' % (host, quota))
        self.consul.kv[host] = quota
    def segments_for_host(self, host):
        segments = [Segment(consul=self.consul, segment_id=k.split('/')[-1], size=v, registry=self) for k, v in self.consul.kv.find("%s/" % host).items()]
        logging.info('Checked for segments assigned to %s: Found %s segment(s)' % (host, len(segments)))
        return segments

def check_consul_health(consul):
    try:
        random.seed(settings['HOSTNAME'])
        random_key = ''.join(random.choice(string.ascii_uppercase + string.digits) for i in range(10))
        logging.error("Inserting random key '%s' into consul's key/value store as a health check." % (random_key,))
        consul.kv[random_key] = True
        del consul.kv[random_key]
    except Exception as e:
        sys.exit('Unable to connect to consul. Exiting to prevent running in a bad state.')

##################################################
# SERVER/MASTER MODE
##################################################

def check_master_config():
    try:
        assert settings['HDFS_PATH'], "HDFS_PATH must be set, otherwise I don't know where to look for sqlite files."
        assert settings['HDFS_HOST'], "HDFS_HOST must be set, or I can't communicate with HDFS."
        assert settings['HDFS_PORT'], "HDFS_PORT must be set, or I can't communicate with HDFS."
        assert settings['ELECTION_CYCLE'] > 0, "ELECTION_CYCLE must be greater than zero. It governs the number of seconds in a sync master election period."
        assert settings['HOSTNAME'], "HOSTNAME must be set, or I can't figure out my own hostname."
        assert settings['EXTERNAL_IP'], "EXTERNAL_IP must be set. We need to know which IP to use."
        assert settings['SYNC_PORT'], "SYNC_PORT must be set. We need to know the output port."
        assert settings['CONSUL_ADDRESS'], "CONSUL_ADDRESS must be set. Where can I contact consul's RPC interface?"
        assert settings['CONSUL_PORT'], "CONSUL_PORT must be set. Where can I contact consul's RPC interface?"
    except AssertionError as e:
        sys.exit("{} Exiting...".format(str(e)))

def hold_election(registry):
    logging.warn('Holding Sync Master Election...')
    sync_master_hosts = registry.consul.catalog.service('trough-sync-master')
    if sync_master_hosts:
        if sync_master_hosts[0]['Node'] == settings['HOSTNAME']:
            # 'touch' the ttl check for sync master
            logging.warn('Still the master. I will check again in %ss' % settings['ELECTION_CYCLE'])
            registry.reset_health_check(pool='sync', service_name='master')
            return True
        logging.warn('I am not the master. I will check again in %ss' % settings['ELECTION_CYCLE'])
        return False
    else:
        logging.warn('There is no "trough-sync-master" service in consul. I am the master.')
        logging.warn('Setting up master service...')
        registry.advertise('trough-sync-master',
            service_id='trough/sync/master',
            port=settings['SYNC_PORT'],
            tags=['master'],
            ttl=settings['ELECTION_CYCLE'] * 3)
        logging.warn('Setting up a health check, ttl %ss...' % (settings['ELECTION_CYCLE'] * 3))
        registry.create_health_check(name='Sync Master Health Check for "%s"' % settings['HOSTNAME'],
                        pool="sync",
                        service_name='master',
                        ttl=settings['ELECTION_CYCLE'] * 3,
                        notes="Sync Servers hold an election every %ss. They are unhealthy after missing 2 elections" % settings['ELECTION_CYCLE'])
        registry.reset_health_check(pool='sync', service_name='master')
        return True

def run_sync_master():
    # TODO: this needs to execute periodically. how? cron? or long-running process?
    ''' 
    "server" mode:
    - if I am not the leader, poll forever
    - if there are hosts to assign to, poll forever.
    - for entire list of segments that match pattern in REMOTE_DATA setting:
        - check consul to make sure each item is assigned to a worker
        - if it is not assigned:
            - assign it, based on the available quota on each worker
        - if the number of assignments for this segment are greater than they should be, and all copies are 'up':
            - unassign the copy with the lowest assignment index
    - for list of hosts:
        - if this host meets a "too empty" metric
            - loop over the segments
            - add extra assignments to the "too empty" host in a ratio of segments which corresponds to the delta from the average load.
    '''
    leader = False
    found_hosts = False
    consul = consulate.Consul(host=settings['CONSUL_ADDRESS'], port=settings['CONSUL_PORT'])
    check_consul_health(consul)
    registry = HostRegistry(consul=consul)

    # hold an election every settings['ELECTION_CYCLE'] seconds
    while not leader:
        leader = hold_election(registry)
        if not leader:
            time.sleep(settings['ELECTION_CYCLE'])
    while not found_hosts:
        logging.warn('Waiting for hosts to join cluster. Sleep period: %ss' % settings['HOST_CHECK_WAIT_PERIOD'])
        found_hosts = registry.look_for_hosts()
        time.sleep(settings['HOST_CHECK_WAIT_PERIOD'])

    # we are assured that we are the master, and that we have machines to assign to.
    sb_client = Client(settings['HDFS_HOST'], settings['HDFS_PORT'])
    logging.info('Connecting to HDFS for file listing on: %s:%s' % (settings['HDFS_HOST'], settings['HDFS_PORT']))

    file_listing = sb_client.ls([settings['HDFS_PATH']])

    file_total = 0
    for file in file_listing:
        local_part = file['path'].split('/')[-1]
        local_part = local_part.replace('.sqlite', '')
        segment = Segment(consul=consul, segment_id=local_part, size=file['length'], registry=registry)
        if not len(segment.all_copies()) >= segment.minimum_assignments():
            emptiest_host = sorted(registry.host_load(), key=lambda host: host['remaining_bytes'], reverse=True)[0]
            # assign the byte count of the file to a key named, e.g. /hostA/segment
            registry.assign(emptiest_host, segment)
        else:
            # If we find too many 'up' copies
            if len(segment.up_copies()) > segment.minimum_assignments():
                # delete the copy with the lowest 'CreateIndex', which records the
                # order in which keys are created.
                assignments = segment.all_copies(full_record=True)
                assignments = sorted(assignments, key=lambda record: record['CreateIndex'])
                host = assignments[0].split("/")[0]
                # remove the assignment
                registry.unassign(host, segment)
        file_total += 1

    logging.info('Rebalancing Hosts...')
    for host in registry.underloaded_hosts():
        # while the load on this host is lower than the acceptable load, reassign 
        # segments in the file listing order returned from snakebite.
        logging('Rebalancing %s (its load is %s, lower than %s, the average)' % (host, host['load_ratio'] * 100, host['average_load_ratio'] * 100))
        ratio_to_reassign = host['average_load_ratio'] - host['load_ratio']
        logging.info('Connecting to HDFS for file listing on: %s:%s' % (settings['HDFS_HOST'], settings['HDFS_PORT']))
        file_listing = sb_client.ls([settings['HDFS_PATH']])
        for file in file_listing:
            local_part = file['path'].split('/')[-1]
            local_part = local_part.replace('.sqlite', '')
            segment = Segment(consul=consul, segment_id=local_part, size=file['length'], registry=registry)
            # if this segment is already assigned to this host, next segment.
            if segment.is_assigned_to_host(host):
                continue
            # add an assignment for this segment to this host.
            registry.assign(host, segment)
            host['assigned_bytes'] += segment.size
            host['load_ratio'] = host['assigned_bytes'] * host['total_bytes']
            if host['load_ratio'] >= host['average_load_ratio'] * 0.95:
                break


##################################################
# LOCAL MODE
##################################################

def check_local_config():
    try:
        assert settings['HOSTNAME'], "HOSTNAME must be set, or I can't figure out my own hostname."
        assert settings['EXTERNAL_IP'], "EXTERNAL_IP must be set. We need to know which IP to use."
        assert settings['READ_PORT'], "SYNC_PORT must be set. We need to know the output port."
        assert settings['CONSUL_ADDRESS'], "CONSUL_ADDRESS must be set. Where can I contact consul's RPC interface?"
        assert settings['CONSUL_PORT'], "CONSUL_PORT must be set. Where can I contact consul's RPC interface?"
    except AssertionError as e:
        sys.exit("{} Exiting...".format(str(e)))

def check_segment_exists(segment):
    logging.info('Checking whether segment "%s" exists on local filesystem in %s' % (segment, settings['LOCAL_DATA']))
    if os.path.isfile(os.path.join(settings['LOCAL_DATA'], "%s.sqlite" % segment)):
        logging.info('Segment "%s" exists' % segment)
        return True
    logging.info('Segment "%s" does not exist' % segment)
    return False

def check_segment_matches_hdfs(sb_client, segment):
    logging.info('Checking that segment %s matches its byte count in HDFS.' % segment)
    segment_filename = os.path.join(settings['LOCAL_DATA'], "%s.sqlite" % segment)
    if os.path.isfile(segment_filename):
        for listing in sb_client.ls([settings['HDFS_PATH']]):
            if listing['length'] == os.path.getsize(segment_filename):
                logging.info('Byte counts match.')
                return True
    logging.warn('Byte counts do not match HDFS for segment %s' % segment)
    return False

def copy_segment_from_hdfs(sb_client, segment):
    logging.info('copying segment %s from HDFS...' % segment)
    source = [os.path.join(settings['HDFS_PATH'], "%s.sqlite" % segment)]
    destination = settings['LOCAL_DATA']
    logging.info('running snakebite.Client.copyToLocal(%s, %s)' % (source, destination))
    for f in sb_client.copyToLocal(source, destination):
        if f['error']:
            logging.error('Error: %s' % f['error'])
        else:
            logging.info('copied %s' % f)

def run_sync_local():
    '''
    "local" mode:
    - if not set up, 
        - set myself up as a host for a consul service as a read or write host depending on settings.
    - reset the countdown on my health check, if it exists
    - figure out what my data limit is (look it up from settings), persisting to consul.
    - query consul for the assignment list for my hostname
    - start 'timer'
    - for each item in the list:
        - check that we have a copy
        - check that the copy we have matches the byte size in hdfs
        - if either check fails:
            - copy file down from hdfs
            - set up a health check (TTL) for this segment, 2 * 'segment_timer'
        - touch segment health check
    - end 'timer'
    - set up a health check (TTL) for myself, 2 * 'timer'
    '''
    my_hostname = settings['HOSTNAME']

    consul = consulate.Consul(host=settings['CONSUL_ADDRESS'], port=settings['CONSUL_PORT'])
    check_consul_health(consul)
    registry = HostRegistry(consul)

    if not registry.host_is_advertised(my_hostname):
        logging.warn('I am not advertised. Advertising myself as "%s".' % my_hostname)
        registry.advertise('trough-read-nodes', service_id='trough/nodes/%s' % my_hostname, tags=[my_hostname])

    # if there is a health check for this node
    if registry.health_check('nodes', my_hostname):
        # reset the countdown
        registry.reset_health_check('nodes', my_hostname)
    logging.warn('Updating health check for "%s".' % my_hostname)
    registry.set_quota(my_hostname, settings['STORAGE_IN_BYTES'])
    loop_timer = time.time()
    sb_client = Client(settings['HDFS_HOST'], settings['HDFS_PORT'])
    for segment in registry.segments_for_host(my_hostname):
        segment_name = segment.id
        exists = check_segment_exists(segment_name)
        matches_hdfs = check_segment_matches_hdfs(sb_client, segment_name)
        if not exists or not matches_hdfs:
            copy_segment_from_hdfs(sb_client, segment_name)
            # to calculate the segment TTL, use (settings['SYNC_LOOP_TIMING'] + total loop time, so far) * 2
            segment_health_ttl = round(settings['SYNC_LOOP_TIMING'] + (loop_timer - time.time()) * 2)
            registry.create_health_check(name='Segment %s Is Healthy' % segment_name,
                            pool="read",
                            service_name=segment_name,
                            ttl=segment_health_ttl,
                            notes="Segment Health Checks occur every %ss. They are unhealthy after missing (appx) 2 sync loops." % settings['SYNC_LOOP_TIMING'])
        registry.advertise('trough-read-segments', service_id='trough/read/%s' % segment_name, tags=[segment_name])
        registry.reset_health_check('read', segment_name)
    if not registry.health_check('nodes', my_hostname):
        # to calculate the node TTL, use (settings['SYNC_LOOP_TIMING'] + total loop time) * 2
        node_health_ttl = round(settings['SYNC_LOOP_TIMING'] + (loop_timer - time.time()) * 2)
        registry.create_health_check(name='Node Health Check for "%s"' % my_hostname,
                        pool="nodes",
                        service_name=my_hostname,
                        ttl=node_health_ttl,
                        notes="Node Health Checks occur every %ss. They are unhealthy after missing (appx) 2 sync loops." % settings['SYNC_LOOP_TIMING'])
        registry.reset_health_check('nodes', my_hostname)



# health check:
# - segment health will be a TTL check, which means the segments are assumed to be 'unhealthy'
#   after some period of time.
# - query consul to see which segments are assigned to this host http://localhost:8500/v1/kv/{{ host }}/?keys
# - for each segment:
#     - check the CRC/MD5 of the file against HDFS
#     - make a query to the file, on localhost HTTP by forcing a Host header. Maybe check that a specific
#       set of tables exists with SELECT name FROM sqlite_master WHERE type='table' AND name='{{ table_name }}';
# - ship a manifest upstream, or possibly for each item 
# for the node health check, receiving any healthy segment message on the host will suffice.
# (aka the health check for DBs should also reset TTL for the host)
    
# health, assignments, and first "up" event:
# - assignments happen via pushing a key/value into the consul store ('/host/segment_id': 'segment_size') 
#   retrieve assignment list via: http://localhost:8500/v1/kv/host1/?keys
# - upon assignment, local synchronizer wakes up, copies file from hdfs.
# - upon copy completion, local synchronizer runs first health check.
# - upon first health check completion, synchronizer sets up DNS for segments on this host.
#   segment is now 'up'
# - upon first health check completion, synchronizer sets up TTL-based future health checks
#   for each segment assigned to this host.

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Run a "server" sync process, which controls other sync processes, ' \
        'or a "local" sync process, which loads segments onto the current machine and performs health checks.')

    parser.add_argument('--server', dest='server', action='store_true',
                        help='run in server or "master" mode, control the actions of other local synchronizers.')
    args = parser.parse_args()
    if args.server:
        check_master_config()
        while True:
            run_sync_master()
    else:
        check_local_config()
        while True:
            run_sync_local()
            time.sleep(settings['SYNC_LOOP_TIMING'])
