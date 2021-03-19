#!/usr/bin/env python3
import trough
from trough.settings import settings
import sqlite3
import ujson
import os
import sqlparse
import logging
import urllib
import doublethink

if settings['SENTRY_DSN']:
    try:
        import sentry_sdk
        sentry_sdk.init(settings['SENTRY_DSN'])
    except ImportError:
        logging.warning("'SENTRY_DSN' setting is configured but 'sentry_sdk' module not available. Install to use sentry.")

class ReadServer:
    def __init__(self):
        self.rethinker = doublethink.Rethinker(db="trough_configuration", servers=settings['RETHINKDB_HOSTS'])
        self.services = doublethink.ServiceRegistry(self.rethinker)
        self.registry = trough.sync.HostRegistry(rethinker=self.rethinker, services=self.services)
        trough.sync.init(self.rethinker)

    def proxy_for_write_host(self, node, segment, query, start_response):
        query = query.encode('utf-8')
        # enforce that we are querying the correct database, send an explicit hostname.
        write_url = "http://{node}:{port}/?segment={segment}".format(node=node, segment=segment.id, port=settings['READ_PORT'])
        try:
            request = urllib.request.Request(write_url, data=query)
            with urllib.request.urlopen(request) as response:
                status_line = '{status_code} {reason}'.format(status_code=response.status_code, reason=response.reason)
                headers = [("Content-Type", response.headers['Content-Type'],)]
                start_response(status_line, headers)
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            print("An error occurred while proxying for host %s for segment %s: %s" % (node, segment, e))

    def sql_result_json_iter(self, cursor):
        first = True
        yield b"["
        try:
            while True:
                row = cursor.fetchone()
                if not row:
                    break
                if not first:
                    yield b",\n"
                output = dict((cursor.description[i][0], value) for i, value in enumerate(row))
                yield ujson.dumps(output, escape_forward_slashes=False).encode('utf-8')
                first = False
            yield b"]\n"
        except Exception as e:
            logging.error('exception in middle of streaming response', exc_info=1)
        finally:
            # close the cursor 'finally', in case there is an Exception.
            cursor.close()
            cursor.connection.close()

    def execute_query(self, segment, query):
        '''Returns a cursor.'''
        logging.info('Servicing request: {query}'.format(query=query))
        # if the user sent more than one query, or the query is not a SELECT, raise an exception.
        if len(sqlparse.split(query)) != 1 or sqlparse.parse(query)[0].get_type() != 'SELECT':
            raise Exception('Exactly one SELECT query per request, please.')
        assert os.path.isfile(segment.local_path())

        logging.info("Connecting to sqlite database: {segment}".format(segment=segment.local_path()))
        connection = sqlite3.connect(segment.local_path())
        trough.sync.setup_connection(connection)
        cursor = connection.cursor()
        cursor.execute(query.decode('utf-8'))
        return cursor

    # uwsgi endpoint
    def __call__(self, env, start_response):
        try:
            query_dict = urllib.parse.parse_qs(env['QUERY_STRING'])
            # use the ?segment= query string variable or the host string to figure out which sqlite database to talk to.
            segment_id = query_dict.get('segment', env.get('HTTP_HOST', "").split("."))[0]
            logging.info('Connecting to Rethinkdb on: %s' % settings['RETHINKDB_HOSTS'])
            segment = trough.sync.Segment(segment_id=segment_id, size=0, rethinker=self.rethinker, services=self.services, registry=self.registry)
            content_length = int(env.get('CONTENT_LENGTH', 0))
            query = env.get('wsgi.input').read(content_length)

            write_lock = segment.retrieve_write_lock()
            if write_lock and write_lock['node'] != settings['HOSTNAME']:
                logging.info('Found write lock for {segment}. Proxying {query} to {host}'.format(segment=segment.id, query=query, host=write_lock['node']))
                return self.proxy_for_write_host(write_lock['node'], segment, query, start_response)

            cursor = self.execute_query(segment, query)
            start_response('200 OK', [('Content-Type','application/json')])
            return self.sql_result_json_iter(cursor)
        except Exception as e:
            logging.error('500 Server Error due to exception', exc_info=True)
            start_response('500 Server Error', [('Content-Type', 'text/plain')])
            return [('500 Server Error: %s\n' % str(e)).encode('utf-8')]

