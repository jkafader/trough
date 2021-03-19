import os
os.environ['TROUGH_SETTINGS'] = os.path.join(os.path.dirname(__file__), "test.conf")

import unittest
from unittest import mock
import trough
import json
import sqlite3
from tempfile import NamedTemporaryFile
from trough import sync
from trough.settings import settings
import doublethink

class TestReadServer(unittest.TestCase):
    def setUp(self):
        self.server = trough.read.ReadServer()
    def test_empty_read(self):
        database_file = NamedTemporaryFile()
        connection = sqlite3.connect(database_file.name)
        cursor = connection.cursor()
        cursor.execute('CREATE TABLE test (id INTEGER PRIMARY KEY AUTOINCREMENT, test varchar(4));')
        # no inserts!
        connection.commit()

        segment = mock.Mock()
        segment.local_path = lambda: database_file.name

        output = b""
        for part in self.server.sql_result_json_iter(
                self.server.execute_query(segment, b'SELECT * FROM "test";')):
            output += part
        output = json.loads(output.decode('utf-8'))
        database_file.close()
        cursor.close()
        connection.close()
        self.assertEqual(output, [])
    def test_read(self):
        database_file = NamedTemporaryFile()
        connection = sqlite3.connect(database_file.name)
        cursor = connection.cursor()
        cursor.execute('CREATE TABLE test (id INTEGER PRIMARY KEY AUTOINCREMENT, test varchar(4));')
        cursor.execute('INSERT INTO test (test) VALUES ("test");')
        connection.commit()
        output = b""

        segment = mock.Mock()
        segment.local_path = lambda: database_file.name

        for part in self.server.sql_result_json_iter(
                self.server.execute_query(segment, b'SELECT * FROM "test";')):
            output += part
        output = json.loads(output.decode('utf-8'))
        cursor.close()
        connection.close()
        database_file.close()
        self.assertEqual(output, [{'id': 1, 'test': 'test'}])
    def test_write_failure(self):
        database_file = NamedTemporaryFile()
        connection = sqlite3.connect(database_file.name)
        cursor = connection.cursor()
        cursor.execute('CREATE TABLE test (id INTEGER PRIMARY KEY AUTOINCREMENT, test varchar(4));')
        cursor.execute('INSERT INTO test (test) VALUES ("test");')
        connection.commit()
        output = b""

        segment = mock.Mock()
        segment.segment_path = lambda: database_file.name

        with self.assertRaises(Exception):
            for item in self.server.read(segment, b'INSERT INTO test (test) VALUES ("test");'):
                print("item:", item)
        database_file.close()
        cursor.close()
        connection.close()
    @mock.patch("trough.read.urllib")
    def test_proxy_for_write_segment(self, urllib):
        class Request(mock.Mock):
            def urlopen(*args, **kwargs):
                response = mock.Mock()
                response.headers = {"Content-Type": "application/json"}
                response.read = lambda: (b"test output")
                response.status_code = 200
                response.reason = "Mocked"
                response.__enter__ = lambda *args, **kwargs: response
                response.__exit__ = lambda *args, **kwargs: None
                return response
        urllib.request.Request = Request
        consul = mock.Mock()
        registry = mock.Mock()
        rethinker = doublethink.Rethinker(db="trough_configuration", servers=settings['RETHINKDB_HOSTS'])
        services = doublethink.ServiceRegistry(rethinker)
        segment = trough.sync.Segment(segment_id="TEST", rethinker=rethinker, services=services, registry=registry, size=0)
        output = self.server.proxy_for_write_host('localhost', segment, "SELECT * FROM mock;", start_response=lambda *args, **kwargs: None)
        self.assertEqual(list(output), [b"test output"])

if __name__ == '__main__':
    unittest.main()
