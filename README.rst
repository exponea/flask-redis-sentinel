Flask-Redis-Sentinel
====================

.. image:: https://travis-ci.org/exponea/flask-redis-sentinel.svg
    :target: https://travis-ci.org/exponea/flask-redis-sentinel
    :alt: Travis CI

Flask-Redis-Sentinel provides support for connecting to Redis using Sentinel and also supports connecting to Redis
without it.

* Supports Python 2.7 and 3.3+
* Licensed using Apache License 2.0

Installation
------------

Install with pip::

    pip install Flask-Redis-Sentinel

Basic usage
-----------

.. code-block:: python

    from flask_redis_sentinel import SentinelExtension

    redis_sentinel = SentinelExtension()
    redis_connection = redis_sentinel.default_connection

    # Later when you create application
    app = Flask(...)
    redis_sentinel.init_app(app)

You can configure Redis connection parameters using `REDIS_URL` Flask configuration variable with `redis+sentinel`
URL scheme::

    redis+sentinel://localhost:26379[,otherhost:26379,...]/mymaster/0
    redis+sentinel://localhost:26379[,otherhost:26379,...]/mymaster/0?socket_timeout=0.1
    redis+sentinel://localhost:26379[,otherhost:26379,...]/mymaster/0?sentinel_socket_timeout=0.1
    redis+sentinel://:sentinel-secret-password@localhost:26379[,otherhost:26379,...]/mymaster/0?sentinel_socket_timeout=0.1

The extension also supports URL schemes as supported by redis-py for connecting to an instance directly without Sentinel::

    redis://[:password]@localhost:6379/0
    rediss://[:password]@localhost:6379/0
    unix://[:password]@/path/to/socket.sock?db=0

Flask-And-Redis style config variables are also supported for easier migration, but the extension will
log a `DeprecationWarning`::

    REDIS_HOST = 'localhost'
    REDIS_PORT = 6379
    REDIS_DB = 0

In case both `REDIS_URL` and other variables are present, the URL is used.

Creating multiple connection pools using a single Sentinel cluster
------------------------------------------------------------------

.. code-block:: python

    from flask_redis_sentinel import SentinelExtension

    redis_sentinel = SentinelExtension()
    master1 = redis_sentinel.master_for('service1')
    master2 = redis_sentinel.master_for('service2')
    slave1 = redis_sentinel.slave_for('service1')

Accessing redis-py's Sentinel instance
--------------------------------------

.. code-block:: python

    from flask_redis_sentinel import SentinelExtension
    from flask import jsonify, Flask

    app = Flask('test')

    redis_sentinel = SentinelExtension(app=app)

    @app.route('/')
    def index():
        slaves = redis_sentinel.sentinel.discover_slaves('service1')
        return jsonify(slaves=slaves)

Change log
----------

v2.1.0
~~~~~~

* Removed the thread-local variable for sentinel connection pool. If you want
  to use sentinel with multiple threads, you need to use a patched
  version of redis-py.
* Added `disconnect()` method for resetting the connection pool

v2.0.1
~~~~~~

* Reupload to PyPI

v2.0.0
~~~~~~

* Connections are now thread-local to avoid race conditions after Redis master failover
* Removed support for `REDIS_{HOST, PORT, DB}` config variables

v1.0.0
~~~~~~

* Moved URL handling code to a separate library, `redis_sentinel_url`
* Backward-incompatible change::

    # Old
    redis+sentinel://host:port/service?slave=true

  Should now be written as::

    # New
    redis+sentinel://host:port/service?client_type=slave

v0.2.0
~~~~~~

* Use config variables other than `REDIS_{HOST, PORT, DB}` even if `REDIS_URL` is used
* Minor refactoring

v0.1.0
~~~~~~

* Initial release
