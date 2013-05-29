# -*- encoding: utf-8 -*-

"""Tests for vumi.blinkenlights.heartbeat.monitor"""

import time
import json

from twisted.trial.unittest import TestCase
from twisted.internet.defer import inlineCallbacks

from vumi.tests.utils import get_stubbed_worker
from vumi.blinkenlights.heartbeat import publisher
from vumi.blinkenlights.heartbeat import monitor
from vumi.blinkenlights.heartbeat.storage import issue_key
from vumi.utils import generate_worker_id


def expected_wkr_dict():
    wkr = {
        'id': 'system-1:foo',
        'name': 'foo',
        'system_id': 'system-1',
        'min_procs': 1,
        'hosts': [{'host': 'host-1', 'proc_count': 1}],
    }
    return wkr


def expected_sys_dict():
    sys = {
        'name': 'system-1',
        'id': 'system-1',
        'timestamp': int(435),
        'workers': [expected_wkr_dict()],
    }
    return sys


class TestWorkerInstance(TestCase):

    def test_create(self):
        worker = monitor.WorkerInstance('foo', 34)
        self.assertEqual(worker.hostname, 'foo')
        self.assertEqual(worker.pid, 34)

    def test_equiv(self):
        self.assertEqual(monitor.WorkerInstance('foo', 34),
                         monitor.WorkerInstance('foo', 34))
        self.failIfEqual(monitor.WorkerInstance('foo', 4),
                         monitor.WorkerInstance('foo', 34))
        self.failIfEqual(monitor.WorkerInstance('fo', 34),
                         monitor.WorkerInstance('foo', 34))

    def test_hash(self):
        worker1 = monitor.WorkerInstance('foo', 34)
        worker2 = monitor.WorkerInstance('foo', 34)
        worker3 = monitor.WorkerInstance('foo', 35)
        worker4 = monitor.WorkerInstance('bar', 34)
        self.assertEqual(hash(worker1), hash(worker2))
        self.assertNotEqual(hash(worker1), hash(worker3))
        self.assertNotEqual(hash(worker1), hash(worker4))


class TestWorker(TestCase):

    def test_to_dict(self):
        wkr = monitor.Worker('system-1', 'foo', 1)
        wkr.reset()
        wkr.record('host-1', 34)

        obj = wkr.to_dict()
        self.assertEqual(cmp(obj, expected_wkr_dict()), 0,
                         "Assertion equal(obj,expected) failed. "
                         "obj=%s expected=%s" % (obj, expected_wkr_dict()))

    def test_compute_host_info(self):
        wkr = monitor.Worker('system-1', 'foo', 1)
        wkr.reset()
        wkr.record('host-1', 34)
        wkr.record('host-1', 546)

        counts = wkr._compute_host_info(wkr._instances)
        self.assertEqual(counts['host-1'], 2)


class TestSystem(TestCase):

    def test_to_dict(self):
        wkr = monitor.Worker('system-1', 'foo', 1)
        sys = monitor.System('system-1', 'system-1', [wkr])
        wkr.reset()
        wkr.record('host-1', 34)
        obj = sys.to_dict()
        obj['timestamp'] = int(435)
        self.assertEqual(cmp(obj, expected_sys_dict()), 0,
                         "Assertion equal(obj,expected) failed. "
                         "obj=%s expected=%s" % (obj, expected_sys_dict()))


class TestHeartBeatMonitor(TestCase):

    def setUp(self):
        config = {
            'deadline': 30,
            'redis_manager': {
                'key_prefix': 'heartbeats',
                'db': 5,
                'FAKE_REDIS': True,
            },
            'monitored_systems': {
                'system-1': {
                    'system_name': 'system-1',
                    'system_id': 'system-1',
                    'workers': {
                        'twitter_transport': {
                            'name': 'twitter_transport',
                            'min_procs': 2,
                        }
                    }
                }
            }
        }
        self.worker = get_stubbed_worker(monitor.HeartBeatMonitor, config)

    def tearDown(self):
        self.worker.stopWorker()

    def gen_fake_attrs(self, timestamp):
        sys_id = 'system-1'
        wkr_name = 'twitter_transport'
        wkr_id = generate_worker_id(sys_id, wkr_name)
        attrs = {
            'version': publisher.HeartBeatMessage.VERSION_20130319,
            'system_id': sys_id,
            'worker_id': wkr_id,
            'worker_name': wkr_name,
            'hostname': "test-host-1",
            'timestamp': timestamp,
            'pid': 345,
        }
        return attrs

    @inlineCallbacks
    def test_update(self):
        """
        Test the processing of a message.

        """

        yield self.worker.startWorker()
        attrs1 = self.gen_fake_attrs(time.time())
        attrs2 = self.gen_fake_attrs(time.time())

        # process the fake message (and process it twice to verify idempotency)
        self.worker.update(attrs1)
        self.worker.update(attrs1)

        # retrieve the instance set corresponding to the worker_id in the
        # fake message
        wkr = self.worker._workers[attrs1['worker_id']]
        self.assertEqual(len(wkr._instances), 1)
        inst = wkr._instances.pop()
        wkr._instances.add(inst)
        self.assertEqual(inst.hostname, "test-host-1")
        self.assertEqual(inst.pid, 345)

        # now process a message from another instance of the worker
        # and verify that there are two recorded instances
        attrs2['hostname'] = 'test-host-2'
        self.worker.update(attrs2)
        self.assertEqual(len(wkr._instances), 2)

    @inlineCallbacks
    def test_audit_fail(self):
        # here we test the verification of a worker who
        # who had less than min_procs check in

        yield self.worker.startWorker()
        fkredis = self.worker._redis

        attrs = self.gen_fake_attrs(time.time())
        wkr_id = attrs['worker_id']
        # process the fake message ()
        self.worker.update(attrs)

        wkr = self.worker._workers[attrs['worker_id']]

        wkr.audit(self.worker._storage)

        # test that an issue was opened
        self.assertEqual(wkr.procs_count, 1)
        key = issue_key(wkr_id)
        issue = json.loads((yield fkredis.get(key)))
        self.assertEqual(issue['issue_type'], 'min-procs-fail')

    @inlineCallbacks
    def test_audit_pass(self):
        # here we test the verification of a worker who
        # who had more than min_procs check in

        yield self.worker.startWorker()
        fkredis = self.worker._redis

        attrs = self.gen_fake_attrs(time.time())
        wkr_id = attrs['worker_id']
        # process the fake message ()
        self.worker.update(attrs)
        attrs['pid'] = 2342
        self.worker.update(attrs)

        wkr = self.worker._workers[attrs['worker_id']]

        wkr.audit(self.worker._storage)

        # verify that no issue has been opened
        self.assertEqual(wkr.procs_count, 2)
        key = issue_key(wkr_id)
        issue = yield fkredis.get(key)
        self.assertEqual(issue, None)

    @inlineCallbacks
    def test_prepare_storage(self):
        yield self.worker.startWorker()
        fkredis = self.worker._redis

        self.worker._prepare_storage()

        # Systems
        systems = yield fkredis.smembers('systems')
        self.assertEqual(tuple(systems), ('system-1',))

    @inlineCallbacks
    def test_serialize_to_redis(self):
        """
        This covers a lot of the serialization methods
        as well as the _sync_to_storage() function.
        """
        yield self.worker.startWorker()
        fkredis = self.worker._redis

        attrs = self.gen_fake_attrs(time.time())

        # process the fake message
        self.worker.update(attrs)

        self.worker._sync_to_storage()

        # this blob is what should be persisted into redis (as JSON)
        expected = {
            u'name': u'system-1',
            u'id': u'system-1',
            u'timestamp': 2,
            u'workers': [{
                    u'id': u'system-1:twitter_transport',
                    u'name': u'twitter_transport',
                    u'system_id': u'system-1',
                    u'min_procs': 2,
                    u'hosts': [{u'host': u'test-host-1', u'proc_count': 1}]
            }],
        }

        # verify that the system data was persisted correctly
        system = json.loads((yield fkredis.get('system:system-1')))
        system['timestamp'] = 2
        self.assertEqual(cmp(system, expected), 0,
                         "Assertion equal(system, expected) failed. "
                         "obj=%s expected=%s" % (system, expected))
