#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

# design:
# A threaded worker accepts connections in the main loop, accepted
# connections are added to the thread pool as a connection job.
# Keepalive connections are put back in the loop waiting for an event.
# If no event happen after the keep alive timeout, the connection is
# closed.
# pylint: disable=no-else-break

from concurrent import futures
import errno
import os
import selectors
import socket
import ssl
import sys
import time
from collections import deque
from datetime import datetime
from functools import partial
from threading import RLock

from . import base
from .. import http
from .. import util
from .. import sock
from ..http import wsgi


class TConn:

    def __init__(self, cfg, sock, client, server):
        self.cfg = cfg
        self.sock = sock
        self.client = client
        self.server = server

        self.timeout = None
        self.parser = None
        self.initialized = False

        # set the socket to non blocking
        self.sock.setblocking(False)

    def init(self):
        self.initialized = True
        self.sock.setblocking(True)

        if self.parser is None:
            # wrap the socket if needed
            if self.cfg.is_ssl:
                self.sock = sock.ssl_wrap_socket(self.sock, self.cfg)

            # initialize the parser
            self.parser = http.RequestParser(self.cfg, self.sock, self.client)

    def set_timeout(self):
        # set the timeout
        self.timeout = time.time() + self.cfg.keepalive

    def close(self):
        util.close(self.sock)


class ThreadWorker(base.Worker):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.worker_connections = self.cfg.worker_connections
        self.max_keepalived = self.cfg.worker_connections - self.cfg.threads
        # initialise the pool
        self.tpool = None
        self.poller = None
        self._lock = None
        self.futures = deque()
        self._keep = deque()
        self.nr_conns = 0

    @classmethod
    def check_config(cls, cfg, log):
        max_keepalived = cfg.worker_connections - cfg.threads

        if max_keepalived <= 0 and cfg.keepalive:
            log.warning("No keepalived connections can be handled. " +
                        "Check the number of worker connections and threads.")

    def init_process(self):
        # tpool 线程池，用于处理请求
        self.tpool: futures.ThreadPoolExecutor = self.get_thread_pool()
        self.poller = selectors.DefaultSelector()
        self._lock = RLock()
        super().init_process()

    def get_thread_pool(self):
        """Override this method to customize how the thread pool is created"""
        return futures.ThreadPoolExecutor(max_workers=self.cfg.threads)

    def handle_quit(self, sig, frame):
        self.alive = False
        # worker_int callback
        self.cfg.worker_int(self)
        self.tpool.shutdown(False)
        time.sleep(0.1)
        sys.exit(0)

    def _wrap_future(self, fs: futures.Future, conn: TConn):
        fs.conn = conn
        self.futures.append(fs)
        fs.add_done_callback(self.finish_request)

    def enqueue_req(self, conn: TConn):
        conn.init()
        # submit the connection to a worker
        fs: futures.Future = self.tpool.submit(self.handle, conn)
        self._wrap_future(fs, conn)

    def accept(self, server, listener):
        try:
            # @gaojian
            # 获取连接请求
            # listener.accept() 返回一个包含两个元素的元组 (conn, address)：
            # - conn 是一个新的套接字对象（socket.socket 类型），表示与客户端的连接。
            # - address 是客户端的地址，通常是一个包含 IP 地址和端口号的元组。
            sock, client = listener.accept()
            # initialize the connection object
            conn = TConn(self.cfg, sock, client, server)

            self.nr_conns += 1
            # wait until socket is readable
            with self._lock:
                # register the socket to the poller
                self.poller.register(conn.sock, selectors.EVENT_READ,
                                     partial(self.on_client_socket_readable, conn))
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.ECONNABORTED,
                               errno.EWOULDBLOCK):
                raise

    def on_client_socket_readable(self, conn, client):
        # gaojian: 处理请求
        with self._lock:
            # unregister the client from the poller

            # @gaojian
            # 当你从事件循环中取消注册一个客户端，操作系统将不再监视该客户端的文件描述符上的事件。这意味着：
            # - 不再接收事件通知：事件循环将不再接收该客户端的任何读、写或错误事件的通知。
            # - 数据处理：如果该客户端仍然活跃并发送数据，操作系统的网络栈仍然会接收这些数据，但你的应用程序将不会被通知这些数据的到来，因为文件描述符已经被取消注册。
            # - 资源释放：如果你关闭了该客户端的文件描述符（例如通过 client.close()），操作系统将释放与该文件描述符相关的资源。

            # TODO: 这里是不是有问题，应该是取消注册conn.sock，而不是client，是吗？
            # 这个client 是什么？
            self.poller.unregister(client)

            if conn.initialized:
                # remove the connection from keepalive
                try:
                    self._keep.remove(conn)
                except ValueError:
                    # race condition
                    return

        # submit the connection to a worker
        # gaojian: 将连接请求提交给线程池处理
        self.enqueue_req(conn)

    def murder_keepalived(self):
        now = time.time()
        while True:
            with self._lock:
                try:
                    # remove the connection from the queue
                    conn = self._keep.popleft()
                except IndexError:
                    break

            delta = conn.timeout - now
            if delta > 0:
                # add the connection back to the queue
                with self._lock:
                    self._keep.appendleft(conn)
                break
            else:
                self.nr_conns -= 1
                # remove the socket from the poller
                with self._lock:
                    try:
                        self.poller.unregister(conn.sock)
                    except OSError as e:
                        if e.errno != errno.EBADF:
                            raise
                    except KeyError:
                        # already removed by the system, continue
                        pass
                    except ValueError:
                        # already removed by the system continue
                        pass

                # close the socket
                conn.close()

    def is_parent_alive(self):
        # If our parent changed then we shut down.
        if self.ppid != os.getppid():
            self.log.info("Parent changed, shutting down: %s", self)
            return False
        return True

    def run(self):
        # init listeners, add them to the event loop
        for sock in self.sockets:
            sock.setblocking(False)
            # a race condition during graceful shutdown may make the listener
            # name unavailable in the request handler so capture it once here
            server = sock.getsockname()
            acceptor = partial(self.accept, server)
            self.poller.register(sock, selectors.EVENT_READ, acceptor)

        while self.alive:
            # notify the arbiter we are alive
            self.notify()

            # can we accept more connections?
            if self.nr_conns < self.worker_connections:
                # wait for an event
                events = self.poller.select(1.0)
                for key, _ in events:
                    callback = key.data
                    callback(key.fileobj)

                # check (but do not wait) for finished requests
                result = futures.wait(self.futures, timeout=0,
                                      return_when=futures.FIRST_COMPLETED)
            else:
                # wait for a request to finish
                result = futures.wait(self.futures, timeout=1.0,
                                      return_when=futures.FIRST_COMPLETED)

            # clean up finished requests
            for fut in result.done:
                self.futures.remove(fut)

            if not self.is_parent_alive():
                break

            # handle keepalive timeouts
            self.murder_keepalived()

        self.tpool.shutdown(False)
        self.poller.close()

        for s in self.sockets:
            s.close()

        futures.wait(self.futures, timeout=self.cfg.graceful_timeout)

    def finish_request(self, fs):
        if fs.cancelled():
            self.nr_conns -= 1
            fs.conn.close()
            return

        try:
            (keepalive, conn) = fs.result()
            # if the connection should be kept alived add it
            # to the eventloop and record it
            if keepalive and self.alive:
                # flag the socket as non blocked
                conn.sock.setblocking(False)

                # register the connection
                conn.set_timeout()
                with self._lock:
                    self._keep.append(conn)

                    # add the socket to the event loop
                    # gaojian: 将 socket 加入到事件循环中，等待新的连接
                    self.poller.register(conn.sock, selectors.EVENT_READ,
                                         partial(self.on_client_socket_readable, conn))
            else:
                self.nr_conns -= 1
                conn.close()
        except Exception:
            # an exception happened, make sure to close the
            # socket.
            self.nr_conns -= 1
            fs.conn.close()

    def handle(self, conn):
        keepalive = False
        req = None
        try:
            req = next(conn.parser)
            if not req:
                return (False, conn)

            # handle the request
            keepalive = self.handle_request(req, conn)
            if keepalive:
                return (keepalive, conn)
        except http.errors.NoMoreData as e:
            self.log.debug("Ignored premature client disconnection. %s", e)

        except StopIteration as e:
            self.log.debug("Closing connection. %s", e)
        except ssl.SSLError as e:
            if e.args[0] == ssl.SSL_ERROR_EOF:
                self.log.debug("ssl connection closed")
                conn.sock.close()
            else:
                self.log.debug("Error processing SSL request.")
                self.handle_error(req, conn.sock, conn.client, e)

        except OSError as e:
            if e.errno not in (errno.EPIPE, errno.ECONNRESET, errno.ENOTCONN):
                self.log.exception("Socket error processing request.")
            else:
                if e.errno == errno.ECONNRESET:
                    self.log.debug("Ignoring connection reset")
                elif e.errno == errno.ENOTCONN:
                    self.log.debug("Ignoring socket not connected")
                else:
                    self.log.debug("Ignoring connection epipe")
        except Exception as e:
            self.handle_error(req, conn.sock, conn.client, e)

        return (False, conn)

    def handle_request(self, req, conn):
        environ = {}
        resp = None
        try:
            self.cfg.pre_request(self, req)
            request_start = datetime.now()
            resp, environ = wsgi.create(req, conn.sock, conn.client,
                                        conn.server, self.cfg)
            environ["wsgi.multithread"] = True
            self.nr += 1
            if self.nr >= self.max_requests:
                if self.alive:
                    self.log.info("Autorestarting worker after current request.")
                    self.alive = False
                resp.force_close()

            if not self.alive or not self.cfg.keepalive:
                resp.force_close()
            elif len(self._keep) >= self.max_keepalived:
                resp.force_close()

            respiter = self.wsgi(environ, resp.start_response)
            try:
                if isinstance(respiter, environ['wsgi.file_wrapper']):
                    resp.write_file(respiter)
                else:
                    for item in respiter:
                        resp.write(item)

                resp.close()
            finally:
                request_time = datetime.now() - request_start
                # 记录 accesslog
                self.log.access(resp, req, environ, request_time)
                if hasattr(respiter, "close"):
                    respiter.close()

            if resp.should_close():
                self.log.debug("Closing connection.")
                return False
        except OSError:
            # pass to next try-except level
            util.reraise(*sys.exc_info())
        except Exception:
            if resp and resp.headers_sent:
                # If the requests have already been sent, we should close the
                # connection to indicate the error.
                self.log.exception("Error handling request")
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)
                    conn.sock.close()
                except OSError:
                    pass
                raise StopIteration()
            raise
        finally:
            try:
                self.cfg.post_request(self, req, environ, resp)
            except Exception:
                self.log.exception("Exception in post_request hook")

        return True
