#!/usr/bin/env python3
import trough
from trough.settings import settings
import sqlite3
import ujson
import os
import sqlparse
import logging
import consulate

class WriteServer:
    def write(self, segment, query):
        logging.info('Servicing request: {query}'.format(query=query))
        # if one or more of the query(s) are not a write query, raise an exception.
        if not query:
            raise Exception("No query provided.")
        for q in sqlparse.parse(query):
            if q.get_type() not in settings['ALLOWED_WRITE_VERBS']:
                raise Exception('This server only accepts "Write" queries that begin with {}.'.format(settings['ALLOWED_WRITE_VERBS']))
        connection = sqlite3.connect(segment.segment_path())
        cursor = connection.cursor()
        try:
            output = cursor.execute(query.decode('utf-8'))
        finally:
            cursor.close()
            connection.commit()
            connection.close()
        return b"OK"

    # uwsgi endpoint
    def __call__(self, env, start_response):
        self.start_response = start_response
        try:
            segment_id = env.get('HTTP_HOST', "").split(".")[0] # get database id from host/request path
            consul = consulate.Consul(host=settings['CONSUL_ADDRESS'], port=settings['CONSUL_PORT'])
            registry = trough.sync.HostRegistry(consul=consul)
            segment = trough.sync.Segment(segment_id=segment_id, consul=consul, registry=registry, size=None)
            query = env.get('wsgi.input').read()

            write_lock = segment.retrieve_write_lock()
            if not write_lock or write_lock['Node'] != settings['HOSTNAME']:
                raise Exception("This node cannot write to segment '{}'. There is no write lock set, or the write lock authorizes another node.".format(segment.id))

            return self.write(segment, query)
        except Exception as e:
            start_response('500 Server Error', [('Content-Type', 'text/plain')])
            return [b'500 Server Error: %s' % str(e).encode('utf-8')]

# uwsgi endpoint
application = WriteServer()