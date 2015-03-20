# Copyright 2015 RethinkDB, all rights reserved.

import errno
import json
import numbers
import socket
import struct
import sys
from tornado import gen, iostream
from tornado.ioloop import IOLoop
from tornado.concurrent import Future

from . import ql2_pb2 as p
from .net import decodeUTF, Query, Response, Cursor, maybe_profile, convert_pseudo
from .net import Connection as ConnectionBase
from . import repl              # For the repl connection
from .errors import *
from .ast import RqlQuery, RqlTopLevelQuery, DB

__all__ = ['Connection']

pResponse = p.Response.ResponseType
pQuery = p.Query.QueryType

@gen.coroutine
def with_absolute_timeout(deadline, generator, io_loop):
    if deadline is None:
        res = yield generator
    else:
        try:
            res = yield gen.with_timeout(deadline, generator, io_loop=io_loop)
        except gen.TimeoutError:
            raise RqlTimeoutError()
    raise gen.Return(res)


class TornadoCursor(Cursor):
    def __init__(self, *args, **kwargs):
        Cursor.__init__(self, *args, **kwargs)
        self.new_response = Future()

    def _extend(self, res):
        Cursor._extend(self, res)
        self.new_response.set_result(True)
        self.new_response = Future()

    def _get_next(self, timeout):
        result_future = Future()
        deadline = None if timeout is None else self.conn._io_loop.time() + timeout

        self._maybe_fetch_batch()
        self._try_next(result_future, deadline)
        return result_future

    def _try_next(self, result_future, deadline):
        if result_future.running():
            if len(self.items) == 0:
                if self.error == False:
                    result_future.set_exception(StopIteration())
                elif self.error is not None:
                    result_future.set_exception(self.error)
                elif deadline is not None and deadline < self.conn._io_loop.time():
                    result_future.set_exception(RqlTimeoutError())
                else:
                    self.conn._io_loop.add_future(self.new_response,
                        lambda future: self._try_next(result_future, None))
                    if deadline is not None:
                        self.conn._io_loop.add_timeout(deadline,
                            TornadoCursor._try_next, self, result_future, deadline)
            else:
                result_future.set_result(convert_pseudo(self.items.pop(0), self.query))


class ConnectionInstance(object):
    def __init__(self, parent, io_loop=None):
        self._parent = parent
        self._closing = False
        self._user_queries = { }
        self._cursor_cache = { }
        self._ready = Future()
        self._io_loop = io_loop
        if self._io_loop is None:
            self._io_loop = IOLoop.current()

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        self._stream = iostream.IOStream(self._socket, io_loop=self._io_loop)

    @gen.coroutine
    def connect(self, timeout):
        deadline = None if timeout is None else self._io_loop.time() + timeout
        try:
            yield with_absolute_timeout(deadline,
                                        self._stream.connect((self._parent.host,
                                                              self._parent.port)),
                                        self._io_loop)
        except Exception as err:
            raise RqlDriverError('Could not connect to %s:%s. Error: %s' %
                    (self._parent.host, self._parent.port, str(err)))

        try:
            self._stream.write(self._parent.handshake)
            response = yield with_absolute_timeout(deadline,
                                                   self._stream.read_until(b'\0'),
                                                   io_loop=self._io_loop)
        except Exception as err:
            raise RqlDriverError(
                'Connection interrupted during handshake with %s:%s. Error: %s' %
                    (self._parent.host, self._parent.port, str(err)))

        message = decodeUTF(response[:-1]).split('\n')[0]

        if message != 'SUCCESS':
            self.close(False, None)
            raise RqlDriverError('Server dropped connection with message: "%s"' %
                               message)

        # Start a parallel function to perform reads
        self._io_loop.add_callback(lambda: self._reader())
        raise gen.Return(self._parent)

    def is_open(self):
        return not self._stream.closed()

    @gen.coroutine
    def close(self, noreply_wait, token, exc_info=False):
        self._closing = True
        if exc_info:
            (_, ex, _) = sys.exc_info()
            err_message = "Connection is closed (%s)." + str(ex)
        else:
            err_message = "Connection is closed."

        for cursor in iter(self._cursor_cache.values()):
            cursor._error(err_message)

        for query, future in iter(self._user_queries.values()):
            future.set_exception(RqlDriverError(err_message))

        self._user_queries = { }
        self._cursor_cache = { }

        if noreply_wait:
            noreply = Query(pQuery.NOREPLY_WAIT, token, None, None)
            yield self.run_query(noreply, False)

        try:
            self._stream.close()
        except iostream.StreamClosedError:
            pass
        raise gen.Return(None)

    @gen.coroutine
    def run_query(self, query, noreply):
        yield self._stream.write(query.serialize())
        if noreply:
            raise gen.Return(None)

        response_future = Future()
        self._user_queries[query.token] = (query, response_future)
        res = yield response_future
        raise gen.Return(res)

    @gen.coroutine
    def _reader(self):
        try:
            while True:
                buf = yield self._stream.read_bytes(12)
                (token, length,) = struct.unpack("<qL", buf)
                buf = yield self._stream.read_bytes(length)
                res = Response(token, buf)

                cursor = self._cursor_cache.get(token)
                if cursor is not None:
                    cursor._extend(res)
                elif token in self._user_queries:
                    # Do not pop the query from the dict until later, so
                    # we don't lose track of it in case of an exception
                    query, future = self._user_queries[token]
                    if res.type == pResponse.SUCCESS_ATOM:
                        value = convert_pseudo(res.data[0], query)
                        future.set_result(maybe_profile(value, res))
                    elif res.type in (pResponse.SUCCESS_SEQUENCE,
                                      pResponse.SUCCESS_PARTIAL):
                        cursor = TornadoCursor(self, query)
                        self._cursor_cache[token] = cursor
                        cursor._extend(res)
                        future.set_result(maybe_profile(cursor, res))
                    elif res.type == pResponse.WAIT_COMPLETE:
                        future.set_result(None)
                    else:
                        future.set_exception(res.make_error(query))
                    del self._user_queries[token]
                elif not self._closing:
                    raise RqlDriverError("Unexpected response received.")
        except:
            if not self._closing:
                self.close(False, None, exc_info=True)


# Wrap functions from the base connection class that may throw - these will
# put any exception inside a Future and return it.
class Connection(ConnectionBase):
    def __init__(self, *args, **kwargs):
        ConnectionBase.__init__(self, ConnectionInstance, *args, **kwargs)

    @gen.coroutine
    def reconnect(self, noreply_wait=True, timeout=None):
        # We close before reconnect so reconnect doesn't try to close us
        # and then fail to return the Future (this is a little awkward).
        yield self.close(noreply_wait)
        res = yield ConnectionBase.reconnect(self, noreply_wait, timeout)
        raise gen.Return(res)

    @gen.coroutine
    def close(self, *args, **kwargs):
        if self._instance is None:
            res = None
        else:
            res = yield ConnectionBase.close(self, *args, **kwargs)
        raise gen.Return(res)

    @gen.coroutine
    def noreply_wait(self, *args, **kwargs):
        res = yield ConnectionBase.noreply_wait(self, *args, **kwargs)
        raise gen.Return(res)

    @gen.coroutine
    def _start(self, *args, **kwargs):
        res = yield ConnectionBase._start(self, *args, **kwargs)
        raise gen.Return(res)

    @gen.coroutine
    def _continue(self, *args, **kwargs):
        res = yield ConnectionBase._continue(self, *args, **kwargs)
        raise gen.Return(res)

    @gen.coroutine
    def _stop(self, *args, **kwargs):
        res = yield ConnectionBase._stop(self, *args, **kwargs)
        raise gen.Return(res)
