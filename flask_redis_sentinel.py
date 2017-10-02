# Copyright 2015, 2016, 2017 Exponea s r.o. <info@exponea.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import six
import inspect
import random
import threading
import logging
import weakref
import redis
import redis.sentinel
import redis_sentinel_url
from redis._compat import nativestr
from flask import current_app
from redis.exceptions import ConnectionError, ReadOnlyError
from werkzeug.local import LocalProxy
from werkzeug.utils import import_string

logger = logging.getLogger(__name__)


_EXTENSION_KEY = 'redissentinel'


class SentinelManagedConnection(redis.Connection):
    def __init__(self, **kwargs):
        self.connection_pool = kwargs.pop('connection_pool')
        self.to_be_disconnected = False
        super(SentinelManagedConnection, self).__init__(**kwargs)

    def __repr__(self):
        pool = self.connection_pool
        s = '%s<service=%s%%s>' % (type(self).__name__, pool.service_name)
        if self.host:
            host_info = ',host=%s,port=%s' % (self.host, self.port)
            s = s % host_info
        return s

    def connect_to(self, address):
        self.host, self.port = address
        super(SentinelManagedConnection, self).connect()
        if self.connection_pool.check_connection:
            self.send_command('PING')
            if nativestr(self.read_response()) != 'PONG':
                raise ConnectionError('PING failed')

    def connect(self):
        if self._sock:
            return  # already connected
        if self.connection_pool.is_master:
            self.connect_to(self.connection_pool.get_master_address())
        else:
            for slave in self.connection_pool.rotate_slaves():
                try:
                    return self.connect_to(slave)
                except ConnectionError:
                    continue
            raise SlaveNotFoundError  # Never be here

    def read_response(self):
        try:
            return super(SentinelManagedConnection, self).read_response()
        except ReadOnlyError:
            if self.connection_pool.is_master:
                # When talking to a master, a ReadOnlyError likely
                # indicates that the previous master that we're still connected
                # to has been demoted to a slave and there's a new master.
                self.to_be_disconnected = True
                raise ConnectionError('The previous master is now a slave')
            raise


class SentinelConnectionPool(redis.ConnectionPool):
    """
    Sentinel backed connection pool.

    If ``check_connection`` flag is set to True, SentinelManagedConnection
    sends a PING command right after establishing the connection.
    """

    def __init__(self, service_name, sentinel_manager, **kwargs):
        kwargs['connection_class'] = kwargs.get(
            'connection_class', SentinelManagedConnection)
        self.is_master = kwargs.pop('is_master', True)
        self.check_connection = kwargs.pop('check_connection', False)
        super(SentinelConnectionPool, self).__init__(**kwargs)
        self.connection_kwargs['connection_pool'] = weakref.proxy(self)
        self.service_name = service_name
        self.sentinel_manager = sentinel_manager

    def __repr__(self):
        return "%s<service=%s(%s)" % (
            type(self).__name__,
            self.service_name,
            self.is_master and 'master' or 'slave',
        )

    def reset(self):
        super(SentinelConnectionPool, self).reset()
        self.master_address = None
        self.slave_rr_counter = None

    def get_master_address(self):
        """Get the address of the current master"""
        master_address = self.sentinel_manager.discover_master(
            self.service_name)
        if self.is_master:
            if master_address != self.master_address:
                self.master_address = master_address
        return master_address

    def rotate_slaves(self):
        """Round-robin slave balancer"""
        slaves = self.sentinel_manager.discover_slaves(self.service_name)
        if slaves:
            if self.slave_rr_counter is None:
                self.slave_rr_counter = random.randint(0, len(slaves) - 1)
            for _ in xrange(len(slaves)):
                self.slave_rr_counter = (
                    self.slave_rr_counter + 1) % len(slaves)
                slave = slaves[self.slave_rr_counter]
                yield slave
        # Fallback to the master connection
        try:
            yield self.get_master_address()
        except MasterNotFoundError:
            pass
        raise SlaveNotFoundError('No slave found for %r' % (self.service_name,))

    def _check_connection(self, connection):
        if connection.to_be_disconnected:
            connection.disconnect()
            self.get_master_address()
            return False
        if self.is_master:
            if self.master_address != (connection.host, connection.port):
                connection.disconnect()
                return False
        return True

    def get_connection(self, command_name, *keys, **options):
        """Get a connection from the pool"""
        self._checkpid()
        while True:
            try:
                connection = self._available_connections.pop()
            except IndexError:
                connection = self.make_connection()
            else:
                if not self._check_connection(connection):
                    continue
            self._in_use_connections.add(connection)
            return connection

    def release(self, connection):
        """Releases the connection back to the pool"""
        self._checkpid()
        if connection.pid != self.pid:
            return
        self._in_use_connections.remove(connection)
        if not self._check_connection(connection):
            return
        self._available_connections.append(connection)


class Sentinel(redis.sentinel.Sentinel):

    def master_for(self, service_name, redis_class=redis.StrictRedis,
                   connection_pool_class=SentinelConnectionPool, **kwargs):
        return super(Sentinel, self).master_for(
            service_name, redis_class=redis_class,
            connection_pool_class=connection_pool_class, **kwargs)

    def slave_for(self, service_name, redis_class=redis.StrictRedis,
                   connection_pool_class=SentinelConnectionPool, **kwargs):
        return super(Sentinel, self).slave_for(
            service_name, redis_class=redis_class,
            connection_pool_class=connection_pool_class, **kwargs)


class RedisSentinelInstance(object):

    def __init__(self, url, client_class, client_options, sentinel_class, sentinel_options):
        self.url = url
        self.client_class = client_class
        self.client_options = client_options
        self.sentinel_class = sentinel_class
        self.sentinel_options = sentinel_options
        self.connection = None
        self.master_connections = {}
        self.slave_connections = {}
        self._connect_lock = threading.Lock()
        self._connect()

    def _connect(self):
        with self._connect_lock:
            if self.connection is not None:
                return self.connection

            conn = redis_sentinel_url.connect(
                self.url,
                sentinel_class=self.sentinel_class, sentinel_options=self.sentinel_options,
                client_class=self.client_class, client_options=self.client_options)
            self.connection = conn
            return conn

    def _iter_connections(self):
        if self.connection is not None:
            for conn in self.connection:
                if conn is not None:
                    yield conn
        for conn in six.itervalues(self.master_connections):
            yield conn
        for conn in six.itervalues(self.slave_connections):
            yield conn

    @property
    def sentinel(self):
        return self._connect()[0]

    @property
    def default_connection(self):
        return self._connect()[1]

    def master_for(self, service_name, **kwargs):
        with self._connect_lock:
            try:
                return self.master_connections[service_name]
            except KeyError:
                pass

        sentinel = self.sentinel
        if sentinel is None:
            msg = 'Cannot get master {} using non-sentinel configuration'
            raise RuntimeError(msg.format(service_name))

        with self._connect_lock:
            try:
                return self.master_connections[service_name]
            except KeyError:
                pass

            conn = sentinel.master_for(service_name, redis_class=self.client_class, **kwargs)
            self.master_connections[service_name] = conn
            return conn

    def slave_for(self, service_name, **kwargs):
        with self._connect_lock:
            try:
                return self.slave_connections[service_name]
            except KeyError:
                pass

        sentinel = self.sentinel
        if sentinel is None:
            msg = 'Cannot get slave {} using non-sentinel configuration'
            raise RuntimeError(msg.format(service_name))

        with self._connect_lock:
            try:
                return self.slave_connections[service_name]
            except KeyError:
                pass

            conn = sentinel.slave_for(service_name, redis_class=self.client_class, **kwargs)
            self.slave_connections[service_name] = conn
            return conn

    def disconnect(self):
        with self._connect_lock:
            for conn in self._iter_connections():
                conn.connection_pool.disconnect()


class RedisSentinel(object):
    """Flask extension that supports connections to master using Redis Sentinel.

    Supported URL types:
      redis+sentinel://
      redis://
      rediss://
      unix://
    """

    def __init__(self, app=None, config_prefix='REDIS', client_class=None, sentinel_class=None):
        self.config_prefix = config_prefix
        self.client_class = client_class
        self.sentinel_class = sentinel_class
        if app is not None:
            self.init_app(app)
        self.sentinel = LocalProxy(lambda: self.get_instance().sentinel)
        self.default_connection = LocalProxy(lambda: self.get_instance().default_connection)

    def init_app(self, app, config_prefix=None, client_class=None, sentinel_class=None):
        config_prefix = config_prefix or self.config_prefix
        app.config.setdefault(config_prefix + '_' + 'URL', 'redis://localhost/0')

        config = self._strip_dict_prefix(app.config, config_prefix + '_')

        extensions = app.extensions.setdefault(_EXTENSION_KEY, {})
        if config_prefix in extensions:
            msg = 'Redis sentinel extension with config prefix {} is already registered'
            raise RuntimeError(msg.format(config_prefix))

        client_class = self._resolve_class(
            config, 'CLASS', 'client_class', client_class, redis.StrictRedis)
        sentinel_class = self._resolve_class(
            config, 'SENTINEL_CLASS', 'sentinel_class', sentinel_class, Sentinel)

        url = config.pop('URL')
        client_options = self._config_from_variables(config, client_class)
        sentinel_options = self._config_from_variables(
            self._strip_dict_prefix(config, 'SENTINEL_'), client_class)

        extensions[config_prefix] = RedisSentinelInstance(
            url, client_class, client_options, sentinel_class, sentinel_options)

        self.config_prefix = config_prefix

    def _resolve_class(self, config, config_key, attr, the_class, default_class):
        if the_class is None:
            the_class = getattr(self, attr)
            if the_class is None:
                the_class = config.get(config_key, default_class)
                if isinstance(the_class, six.string_types):
                    the_class = import_string(the_class)
        config.pop(config_key, None)
        return the_class

    @staticmethod
    def _strip_dict_prefix(orig, prefix):
        return {k[len(prefix):]: v for (k, v) in six.iteritems(orig) if k.startswith(prefix)}

    @staticmethod
    def _config_from_variables(config, the_class):
        args = inspect.getargspec(the_class.__init__).args
        args.remove('self')
        args.remove('host')
        args.remove('port')
        args.remove('db')

        return {arg: config[arg.upper()] for arg in args if arg.upper() in config}

    def get_instance(self):
        app = current_app._get_current_object()
        if _EXTENSION_KEY not in app.extensions or self.config_prefix not in app.extensions[_EXTENSION_KEY]:
            msg = 'Redis sentinel extension with config prefix {} was not initialized for application {}'
            raise RuntimeError(msg.format(self.config_prefix, app.import_name))
        return app.extensions[_EXTENSION_KEY][self.config_prefix]

    def master_for(self, service_name, **kwargs):
        return LocalProxy(lambda: self.get_instance().master_for(service_name, **kwargs))

    def slave_for(self, service_name, **kwargs):
        return LocalProxy(lambda: self.get_instance().slave_for(service_name, **kwargs))

    def disconnect(self):
        return self.get_instance().disconnect()


SentinelExtension = RedisSentinel  # for backwards-compatibility
