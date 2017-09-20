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
import redis
import redis.sentinel
import redis_sentinel_url
from flask import current_app
from werkzeug.local import Local, LocalProxy
from werkzeug.utils import import_string


_EXTENSION_KEY = 'redissentinel'


class RedisSentinelInstance(object):

    def __init__(self, url, client_class, client_options, sentinel_class, sentinel_options):
        self.url = url
        self.client_class = client_class
        self.client_options = client_options
        self.sentinel_class = sentinel_class
        self.sentinel_options = sentinel_options
        self.local = Local()
        self._connect()
        if self.local.connection[0] is None:
            # if there is no sentinel, we don't need to use thread-local storage
            self.connection = self.local.connection
            self.local = self

    def _connect(self):
        try:
            return self.local.connection
        except AttributeError:
            conn = redis_sentinel_url.connect(
                self.url,
                sentinel_class=self.sentinel_class, sentinel_options=self.sentinel_options,
                client_class=self.client_class, client_options=self.client_options)
            self.local.connection = conn
            return conn

    @property
    def sentinel(self):
        return self._connect()[0]

    @property
    def default_connection(self):
        return self._connect()[1]

    def master_for(self, service_name, **kwargs):
        try:
            return self.local.master_connections[service_name]
        except AttributeError:
            self.local.master_connections = {}
        except KeyError:
            pass

        sentinel = self.sentinel
        if sentinel is None:
            msg = 'Cannot get master {} using non-sentinel configuration'
            raise RuntimeError(msg.format(service_name))

        conn = sentinel.master_for(service_name, redis_class=self.client_class, **kwargs)
        self.local.master_connections[service_name] = conn
        return conn

    def slave_for(self, service_name, **kwargs):
        try:
            return self.local.slave_connections[service_name]
        except AttributeError:
            self.local.slave_connections = {}
        except KeyError:
            pass

        sentinel = self.sentinel
        if sentinel is None:
            msg = 'Cannot get slave {} using non-sentinel configuration'
            raise RuntimeError(msg.format(service_name))

        conn = sentinel.slave_for(service_name, redis_class=self.client_class, **kwargs)
        self.local.slave_connections[service_name] = conn
        return conn


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
            config, 'SENTINEL_CLASS', 'sentinel_class', sentinel_class, redis.sentinel.Sentinel)

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


SentinelExtension = RedisSentinel  # for backwards-compatibility
