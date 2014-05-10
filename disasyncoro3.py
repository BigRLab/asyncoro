"""This file is part of asyncoro; see http://asyncoro.sourceforge.net
for details.

This module adds API for distributed programming to AsynCoro.
"""

import time
import socket
import inspect
import traceback
import sys
import os
import stat
import hashlib
import random
import collections
import copy

import asyncoro3 as asyncoro
from asyncoro3 import *

__version__ = asyncoro.__version__
__all__ = asyncoro.__all__ + ['RCI']

class _NetRequest(object):
    """Internal use only.
    """

    __slots__ = ('name', 'kwargs', 'src', 'dst', 'auth', 'event', 'id', 'reply', 'timeout')

    def __init__(self, name, kwargs={}, src=None, dst=None, auth=None, timeout=None):
        self.name = name
        self.kwargs = kwargs
        self.src = src
        self.dst = dst
        self.auth = auth
        self.id = None
        self.event = None
        self.reply = None
        self.timeout = timeout

    def __getstate__(self):
        state = {'name':self.name, 'kwargs':self.kwargs, 'src':self.src, 'dst':self.dst,
                 'auth':self.auth, 'id':self.id, 'reply':self.reply, 'timeout':self.timeout}
        return state

    def __setstate__(self, state):
        for k, v in state.items():
            setattr(self, k, v)

class _Peer(object):
    """Internal use only.
    """

    __slots__ = ('location', 'auth', 'keyfile', 'certfile', 'stream', 'conn',
                 'reqs', 'reqs_pending', 'req_coro')

    peers = {}

    def __init__(self, location, auth, keyfile, certfile):
        self.location = location
        self.auth = auth
        self.keyfile = keyfile
        self.certfile = certfile
        self.stream = False
        self.conn = None
        self.reqs = collections.deque()
        self.reqs_pending = Event()
        _Peer.peers[(location.addr, location.port)] = self
        self.req_coro = Coro(self.req_proc)

    @staticmethod
    def send_req(req):
        peer = _Peer.peers.get((req.dst.addr, req.dst.port), None)
        if peer is None:
            logger.debug('invalid peer: %s, %s' % (req.dst, req.name))
            return -1
        peer.reqs.append(req)
        peer.reqs_pending.set()
        return 0

    def req_proc(self, coro=None):
        coro.set_daemon()
        while 1:
            if not self.reqs:
                if not self.stream and self.conn:
                    self.conn.close()
                    self.conn = None
                self.reqs_pending.clear()
                yield self.reqs_pending.wait()
            req = self.reqs.popleft()
            if not self.conn:
                self.conn = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                                        keyfile=self.keyfile, certfile=self.certfile)
                if req.timeout:
                    self.conn.settimeout(req.timeout)
                try:
                    yield self.conn.connect((self.location.addr, self.location.port))
                except:
                    # TODO: delete peer?
                    self.conn = None
                    req.reply = None
                    if req.event:
                        req.event.set()
                    continue
            else:
                self.conn.settimeout(req.timeout)

            req.auth = self.auth
            try:
                yield self.conn.send_msg(serialize(req))
                reply = yield self.conn.recv_msg()
                req.reply = unserialize(reply)
            except socket.error as exc:
                logger.debug('could not send "%s" to %s', req.name, self.location)
                # logger.debug(traceback.format_exc())
                if len(exc.args) == 1 and exc.args[0] == 'hangup':
                    logger.warning('peer "%s" not reachable' % self.location)
                    # TODO: remove peer?
                self.conn.close()
                self.conn = None
                req.reply = None
            except socket.timeout:
                self.conn.close()
                self.conn = None
                req.reply = None
            except:
                # logger.debug(traceback.format_exc())
                self.conn.close()
                self.conn = None
                req.reply = None
            finally:
                if req.event:
                    req.event.set()

    @staticmethod
    def remove(location):
        peer = _Peer.peers.pop((location.addr, location.port), None)
        if peer:
            peer.req_coro.terminate()

class RCI(object):
    """Remote Coro (Callable) Interface.

    Methods registered with RCI can be executed as coroutines on
    request (by remotely running coroutines).
    """

    __slots__ = ('_name', '_location', '_method')

    _asyncoro = None

    def __init__(self, method, name=None):
        """'method' must be generator method; this is used to create
        coroutines. If 'name' is not given, method's function name is
        used for registering.
        """
        self._method = method
        if name:
            self._name = name
        elif inspect.isgeneratorfunction(method):
            self._name = method.__name__
        else:
            self._name = None
        if RCI._asyncoro is None:
            RCI._asyncoro = AsynCoro.instance()
        self._location = RCI._asyncoro._location

    @property
    def name(self):
        """Get name of RCI.
        """
        return self._name

    @staticmethod
    def locate(name, location=None, timeout=None):
        """Must be used with 'yield' as
        'rci = yield RCI.locate("name")'.

        Returns RCI instance to registered RCI at a remote peer so
        its method can be used to execute coroutines at that peer.

        If 'location' is given, RCI is looked up at that specific
        peer; otherwise, all known peers are queried for given name.
        """
        if RCI._asyncoro is None:
            RCI._asyncoro = AsynCoro.instance()
        if location is None:
            req = _NetRequest('locate_rci', kwargs={'name':name},
                              src=RCI._asyncoro._location, timeout=None)
            req.event = Event()
            req.id = id(req)
            RCI._asyncoro._requests[req.id] = req
            for (addr, port), peer in list(_Peer.peers.items()):
                if req.event.is_set():
                    break
                yield RCI._asyncoro._async_reply(req, peer, dst=Location(addr, port))
            else:
                if (yield req.event.wait(timeout)) is False:
                    RCI._asyncoro._requests.pop(req.id, None)
                    req.reply = None
            rci = req.reply
        else:
            req = _NetRequest('locate_rci', kwargs={'name':name}, dst=location, timeout=timeout)
            rci = yield RCI._asyncoro._sync_reply(req)
        raise StopIteration(rci)

    def register(self, name=None):
        """RCI must be registered so it can be located.
        """
        if self._location != RCI._asyncoro._location:
            return -1
        if not inspect.isgeneratorfunction(self._method):
            return -1
        RCI._asyncoro._lock.acquire()
        if not name:
            name = self._name
        else:
            self._name = name
        if RCI._asyncoro._rcis.get(name, None) is None:
            RCI._asyncoro._rcis[name] = self
            RCI._asyncoro._lock.release()
            return 0
        else:
            RCI._asyncoro._lock.release()
            return -1

    def unregister(self):
        """Unregister registered RCI; see 'register' above.
        """
        if self._location != RCI._asyncoro._location:
            return -1
        RCI._asyncoro._lock.acquire()
        if RCI._asyncoro._rcis.pop(self._name, None) is None:
            RCI._asyncoro._lock.release()
            return -1
        else:
            RCI._asyncoro._lock.release()
            return 0

    def __call__(self, *args, **kwargs):
        """Must be used with 'yeild' as 'rcoro = yield rci(*args, **kwargs)'.

        Run RCI (method at remote location) with args and kwargs. Both
        args and kwargs must be serializable. Returns (remote) Coro
        instance.
        """
        req = _NetRequest('run_rci', kwargs={'name':self._name, 'args':args, 'kwargs':kwargs},
                          dst=self._location, timeout=2)
        reply = yield RCI._asyncoro._sync_reply(req)
        if isinstance(reply, Coro):
            raise StopIteration(reply)
        elif reply is None:
            raise StopIteration(None)
        else:
            raise Exception(reply)

    def __getstate__(self):
        state = {'_name':self._name, '_location':self._location}
        return state

    def __setstate__(self, state):
        self._name = state['_name']
        self._location = state['_location']

class AsynCoro(asyncoro.AsynCoro, metaclass=MetaSingleton):
    """Coroutine scheduler. Methods starting with '_' are for internal
    use only.

    If either 'node' or 'udp_port' is not None, asyncoro runs network
    services so distributed coroutines can exhcnage messages. If
    'node' is not None, it must be either hostname or IP address where
    asyncoro runs network services. If 'udp_port' is not None, it is
    port number where asyncoro runs network services. If 'udp_port' is
    0, the default port number 51350 is used. If multiple instances of
    asyncoro are to be running on same host, they all can be started
    with the same 'udp_port', so that asyncoro instances automatically
    find each other.

    'name' is used in locating peers. They must be unique. If used in
    network mode and 'name' is not given, it is set to string
    'node:port'.

    'ext_ip_addr' is the IP address of NAT firewall/gateway if
    asyncoro is behind that firewall/gateway.

    'dest_path_prefix' is path to directory (folder) where transferred
    files are saved. If path doesn't exist, asyncoro creates
    directory with that path. Senders may specify 'dest_path' with
    'send_file', in which case target file will be saved under
    dest_path_prefix + dest_path.

    'max_file_size' is maximum length of file in bytes allowed for
    transferred files. If it is 0 or None (default), there is no
    limit.
    """

    __instance = None

    def __init__(self, udp_port=None, tcp_port=0, node=None, ext_ip_addr=None,
                 name=None, secret=None, certfile=None, keyfile=None, notifier=None,
                 dest_path_prefix=None, max_file_size=None):
        if self.__class__.__instance is None:
            super(AsynCoro, self).__init__(notifier=notifier)
            self.__class__.__instance = self
            self._name = name
            if node:
                node = socket.gethostbyname(node)
            else:
                node = socket.gethostbyname(socket.gethostname())
            self._stream_peers = {}
            self._rcoros = {}
            self._rchannels = {}
            self._rcis = {}
            self._requests = {}
            if not dest_path_prefix:
                dest_path_prefix = os.path.join(os.sep, 'tmp', 'asyncoro')
            self.dest_path_prefix = os.path.abspath(dest_path_prefix)
            if not os.path.isdir(self.dest_path_prefix):
                os.makedirs(self.dest_path_prefix)
            self.max_file_size = max_file_size
            if not udp_port:
                udp_port = 51350
            self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if hasattr(socket, 'SO_REUSEADDR'):
                self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, 'SO_REUSEPORT'):
                self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            self._udp_sock.bind(('', udp_port))
            self._tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if tcp_port:
                if hasattr(socket, 'SO_REUSEADDR'):
                    self._tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, 'SO_REUSEPORT'):
                    self._tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            self._tcp_sock.bind((node, tcp_port))
            self._location = Location(*self._tcp_sock.getsockname())
            if not self._location.port:
                raise Exception('could not start network server at %s' % (self._location))
            if ext_ip_addr:
                try:
                    ext_ip_addr = socket.gethostbyname(ext_ip_addr)
                except:
                    logger.warning('invalid ext_ip_addr ignored')
                else:
                    self._location.addr = ext_ip_addr
            if not name:
                self._name = str(self._location)

            self._secret = secret
            if secret is None:
                self._signature = None
                self._auth_code = None
            else:
                self._signature = ''.join(hex(x)[2:] for x in os.urandom(20))
                self._auth_code = hashlib.sha1(bytes(self._signature + secret, 'ascii')).hexdigest()
            self._certfile = certfile
            self._keyfile = keyfile
            self._tcp_sock.listen(32)
            logger.info('network server "%s" at %s, udp_port=%s, tcp_port=%s',
                        self._name, self._location.addr, self._udp_sock.getsockname()[1],
                        self._location.port)
            self._tcp_sock = AsyncSocket(self._tcp_sock, keyfile=self._keyfile,
                                         certfile=self._certfile)
            self._tcp_coro = Coro(self._tcp_proc)
            if self._udp_sock:
                self._udp_sock = AsyncSocket(self._udp_sock)
                self._udp_coro = Coro(self._udp_proc)

    @property
    def name(self):
        """Get name of AsynCoro.
        """
        return self._name

    def locate(self, name, timeout=None):
        """Must be used with 'yield' as
        'loc = yield scheduler.locate("peer")'.

        Find and return location of peer with 'name'.
        """
        req = _NetRequest('locate_peer', kwargs={'name':name}, src=self._location, timeout=None)
        req.event = Event()
        req.id = id(req)
        self._requests[req.id] = req
        for (addr, port), peer in list(_Peer.peers.items()):
            if req.event.is_set():
                break
            yield self._async_reply(req, peer, dst=Location(addr, port))
        else:
            if (yield req.event.wait(timeout)) is False:
                self._requests.pop(req.id, None)
                req.reply = None
        loc = req.reply
        raise StopIteration(loc)

    def peer(self, node, udp_port=0, tcp_port=0, stream_send=False):
        """Must be used with 'yield', as
        'status = yield scheduler.peer("node1")'.

        Add asyncoro running at node, udp_port as peer to
        communicate. Peers on a local network can find each other
        automatically, but if they are on different networks, 'peer'
        can be used so they find each other.

        If 'tcp_port' is set, asyncoro will contact peer at given port
        (on 'node') using TCP, so UDP is not needed to discover node.

        If 'stream_send' is True, this asyncoro uses same connection
        again and again to send messages (i.e., as a stream) to peer
        'node' (instead of one message per connection). If 'tcp_port'
        is 0, then messages to all asyncoro instances on the given
        node will be streamed. If 'tcp_port' is a port number, then
        messages to only asyncoro running on that port will be
        streamed.
        """
        try:
            node = socket.gethostbyname(node)
        except:
            logger.warning('invalid node: "%s"', str(node))
            raise StopIteration(-1)
        if not udp_port:
            udp_port = 51350
        ping_msg = {'location':self._location, 'signature':self._signature, 'version':__version__}
        ping_msg = b'ping:' + serialize(ping_msg)
        stream_peers = [(addr, port, peer) for (addr, port), peer in _Peer.peers.items() \
                        if (addr == node and (tcp_port == 0 or tcp_port == port))]
        if stream_send:
            for addr, port, peer in stream_peers:
                peer.stream = True
            self._stream_peers[(node, tcp_port)] = True
        else:
            for addr, port, peer in stream_peers:
                peer.stream = False
                self._stream_peers.pop((addr, port), None)
            self._stream_peers.pop((node, tcp_port), None)
        sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        sock.settimeout(2)
        try:
            yield sock.sendto(ping_msg, (node, udp_port))
        except:
            pass
        sock.close()

        if tcp_port:
            req = _NetRequest('ping', kwargs={'peer':self._location, 'signature':self._signature,
                                              'version':__version__}, dst=Location(node, tcp_port))
            sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            sock.settimeout(2)
            try:
                yield sock.connect((node, tcp_port))
                yield sock.send_msg(serialize(req))
            except:
                pass
            sock.close()

        raise StopIteration(0)

    def send_file(self, location, file, dest_path=None, overwrite=False, timeout=None):
        """Must be used with 'yield' as
        'loc = yield scheduler.send_file(location, "file1")'.

        Transfer 'file' to peer at 'location'. If 'dest_path' is not
        None, it must be a relative path (not absolute path), in which
        case, file will be saved at peer's dest_path_prefix +
        dest_path. Returns -1 in case of error, 0 if the file is
        transferred, 1 if the same file is already at the destination
        with same size, timestamp and permissions (so file is not
        transferred) and os.stat structure if a file with same name is
        at the destination with different size/timestamp/permissions,
        but 'overwrite' is False. If return value is 0, the sender may
        want to delete file with 'del_file' later.
        """
        try:
            stat_buf = os.stat(file)
        except:
            raise StopIteration(-1)
        if not ((stat.S_IMODE(stat_buf.st_mode) & stat.S_IREAD) and stat.S_ISREG(stat_buf.st_mode)):
            raise StopIteration(-1)
        if isinstance(dest_path, str) and dest_path:
            dest_path = dest_path.strip()
            # reject absolute path for dest_path
            if os.path.join(os.sep, dest_path) == dest_path:
                raise StopIteration(-1)
        peer = _Peer.peers.get((location.addr, location.port), None)
        if peer is None:
            logger.debug('%s is not a valid peer', location)
            raise StopIteration(-1)
        kwargs = {'file':os.path.basename(file), 'stat_buf':stat_buf,
                  'overwrite':overwrite == True, 'dest_path':dest_path}
        req = _NetRequest('send_file', kwargs=kwargs, dst=location, timeout=timeout)
        sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                           keyfile=self._keyfile, certfile=self._certfile)
        try:
            yield sock.connect((location.addr, location.port))
            req.auth = peer.auth
            yield sock.send_msg(serialize(req))
            reply = yield sock.recv_msg()
            reply = unserialize(reply)
            if reply == 0:
                fd = open(file, 'rb')
                while True:
                    data = fd.read(1024000)
                    if not data:
                        break
                    yield sock.sendall(data)
                fd.close()
                resp = yield sock.recv_msg()
                resp = unserialize(resp)
                if resp == 0:
                    reply = 0
                else:
                    reply = -1
        except socket.error as exc:
            reply = -1
            logger.debug('could not send "%s" to %s', req.name, location)
            if len(exc.args) == 1 and exc.args[0] == 'hangup':
                logger.warning('peer "%s" not reachable' % location)
                # TODO: remove peer?
        except socket.timeout:
            raise StopIteration(-1)
        except:
            reply = -1
        finally:
            sock.close()
        raise StopIteration(reply)

    def del_file(self, location, file, dest_path=None, timeout=None):
        """Must be used with 'yield' as
        'loc = yield scheduler.del_file(location, "file1")'.

        Delete 'file' from peer at 'location'. 'dest_path' must be
        same as that used for 'send_file'.
        """
        if isinstance(dest_path, str) and dest_path:
            dest_path = dest_path.strip()
            # reject absolute path for dest_path
            if os.path.join(os.sep, dest_path) == dest_path:
                raise StopIteration(-1)
        kwargs = {'file':os.path.basename(file), 'dest_path':dest_path}
        req = _NetRequest('del_file', kwargs=kwargs, dst=location, timeout=timeout)
        reply = yield self._sync_reply(req)
        if reply is None:
            reply = -1
        raise StopIteration(reply)

    def _tcp_proc(self, coro=None):
        """Internal use only.
        """
        coro.set_daemon()
        while True:
            conn, addr = yield self._tcp_sock.accept()
            Coro(self._tcp_task, conn, addr)

    def _udp_proc(self, coro=None):
        """Internal use only.
        """
        coro.set_daemon()
        ping_sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        ping_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        ping_sock.settimeout(2)
        ping_msg = {'location':self._location, 'signature':self._signature, 'version':__version__}
        ping_msg = b'ping:' + serialize(ping_msg)
        try:
            yield ping_sock.sendto(ping_msg, ('<broadcast>', self._udp_sock.getsockname()[1]))
        except:
            pass
        ping_sock.close()

        while True:
            msg, addr = yield self._udp_sock.recvfrom(1024)
            if not msg.startswith(b'ping:'):
                logger.warning('ignoring UDP message from %s:%s', addr[0], addr[1])
                continue
            try:
                info = unserialize(msg[len(b'ping:'):])
                assert info['version'] == __version__
                req_peer = info['location']
                if self._secret is None:
                    auth_code = None
                else:
                    auth_code = hashlib.sha1(bytes(info['signature'] + self._secret,
                                                   'ascii')).hexdigest()
                if info['location'] == self._location:
                    continue
                peer = _Peer.peers.get((req_peer.addr, req_peer.port), None)
                if peer and peer.auth == auth_code:
                    continue
            except:
                continue

            req = _NetRequest('ping', kwargs={'peer':self._location, 'signature':self._signature,
                                              'version':__version__}, dst=req_peer, auth=auth_code)
            sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                               keyfile=self._keyfile, certfile=self._certfile)
            sock.settimeout(2)
            try:
                yield sock.connect((req_peer.addr, req_peer.port))
                yield sock.send_msg(serialize(req))
            except:
                pass
            sock.close()

    def _tcp_task(self, conn, addr, coro=None):
        """Internal use only.
        """
        while True:
            msg = yield conn.recv_msg()
            if not msg:
                break
            req = None
            try:
                req = unserialize(msg)
                assert req.auth == self._auth_code
            except:
                if not req:
                    logger.debug('invalid message from %s:%s' % (addr[0], addr[1]))
                    break
                if req.name != 'ping':
                    logger.warning('invalid request %s from %s ignored: "%s", "%s"',
                                   req.name, req.src, req.auth, self._auth_code)
                    break

            if req.dst is not None and req.dst != self._location:
                logger.debug('invalid request "%s" to %s, %s (%s), %s',
                             req.name, req.src, req.dst, self._location, req.id)
                break

            if req.src == self._location:
                async_reply = req
                req = self._requests.pop(async_reply.id, None)
                if req is None:
                    logger.debug('ignoring request "%s"/%s', async_reply.name, async_reply.id)
                    break
                req.reply = async_reply.reply
                del async_reply
                req.event.set()
                break

            if req.name == 'send':
                # synchronous message
                assert req.src is None
                reply = -1
                if req.dst != self._location:
                    logger.warning('ignoring invalid "send" (%s != %s)' % (req.dst, self._location))
                else:
                    cid = req.kwargs.get('coro', None)
                    if cid is not None:
                        coro = self._coros.get(int(cid), None)
                        if coro is not None:
                            reply = coro.send(req.kwargs['message'])
                        else:
                            logger.warning('ignoring message to invalid coro %s', cid)
                    else:
                        name = req.kwargs.get('name', None)
                        if name is not None:
                            channel = self._channels.get(name, None)
                            if channel is not None:
                                reply = channel.send(req.kwargs['message'])
                            else:
                                logger.warning('ignoring message to channel "%s"', name)
                        else:
                            logger.warning('ignoring invalid recipient to "send"')
                yield conn.send_msg(serialize(reply))
            elif req.name == 'deliver':
                # synchronous message
                assert req.src is None
                reply = -1
                if req.dst != self._location:
                    logger.warning('ignoring invalid "deliver" (%s != %s)' % (req.dst, self._location))
                else:
                    cid = req.kwargs.get('coro', None)
                    if cid is not None:
                        coro = self._coros.get(int(cid), None)
                        if coro is not None:
                            coro.send(req.kwargs['message'])
                            reply = 1
                        else:
                            logger.warning('ignoring message to invalid coro %s', cid)
                    else:
                        name = req.kwargs.get('name', None)
                        if name is not None:
                            channel = self._channels.get(name, None)
                            if channel is not None:
                                reply = yield channel.deliver(
                                    req.kwargs['message'], timeout=req.timeout, n=req.kwargs['n'])
                            else:
                                logger.warning('ignoring message to channel "%s"', name)
                        else:
                            logger.warning('ignoring invalid recipient to "send"')
                yield conn.send_msg(serialize(reply))
            elif req.name == 'run_rci':
                # synchronous message
                assert req.src is None
                if req.dst != self._location:
                    reply = Exception('invalid RCI invocation')
                else:
                    rci = self._rcis.get(req.kwargs['name'], None)
                    if rci is None:
                        reply = Exception('RCI "%s" is not registered' % req.kwargs['name'])
                    else:
                        args = req.kwargs['args']
                        kwargs = req.kwargs['kwargs']
                        try:
                            reply = Coro(rci._method, *args, **kwargs)
                        except:
                            reply = Exception(traceback.format_exc())
                yield conn.send_msg(serialize(reply))
            elif req.name == 'locate_channel':
                channel = self._rchannels.get(req.kwargs['name'], None)
                if channel is not None or req.dst == self._location:
                    if req.src:
                        peer = _Peer.peers.get((req.src.addr, req.src.port), None)
                        if peer:
                            sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                                               keyfile=self._keyfile, certfile=self._certfile)
                            try:
                                yield sock.connect((req.src.addr, req.src.port))
                                req.auth = peer.auth
                                req.reply = channel
                                yield sock.send_msg(serialize(req))
                            except:
                                pass
                            sock.close()
                    else:
                        yield conn.send_msg(serialize(channel))
            elif req.name == 'locate_coro':
                coro = self._rcoros.get(req.kwargs['name'], None)
                if coro is not None or req.dst == self._location:
                    if req.src:
                        peer = _Peer.peers.get((req.src.addr, req.src.port), None)
                        if peer:
                            sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                                               keyfile=self._keyfile, certfile=self._certfile)
                            try:
                                yield sock.connect((req.src.addr, req.src.port))
                                req.auth = peer.auth
                                req.reply = coro
                                yield sock.send_msg(serialize(req))
                            except:
                                pass
                            sock.close()
                    else:
                        yield conn.send_msg(serialize(coro))
            elif req.name == 'locate_rci':
                rci = self._rcis.get(req.kwargs['name'], None)
                if rci is not None or req.dst == self._location:
                    if req.src:
                        peer = _Peer.peers.get((req.src.addr, req.src.port), None)
                        if peer:
                            sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                                               keyfile=self._keyfile, certfile=self._certfile)
                            try:
                                yield sock.connect((req.src.addr, req.src.port))
                                req.auth = peer.auth
                                req.reply = rci
                                yield sock.send_msg(serialize(req))
                            except:
                                pass
                            sock.close()
                    else:
                        yield conn.send_msg(serialize(rci))
            elif req.name == 'subscribe':
                # synchronous message
                assert req.src is None
                assert req.dst == self._location
                reply = -1
                channel = self._rchannels.get(req.kwargs['name'], None)
                if channel is not None and channel._location == self._location:
                    subscriber = None
                    coro = req.kwargs.get('coro', None)
                    if coro is not None:
                        subscriber = coro
                    else:
                        rchannel = req.kwargs.get('channel', None)
                        if rchannel is not None:
                            subscriber = rchannel
                    if subscriber is not None:
                        reply = yield channel.subscribe(subscriber)
                yield conn.send_msg(serialize(reply))
            elif req.name == 'monitor':
                # synchronous message
                assert req.src is None
                assert req.dst == self._location
                reply = -1
                rcoro = req.kwargs.get('coro', None)
                monitor = req.kwargs.get('monitor', None)
                if isinstance(rcoro, Coro) and isinstance(monitor, Coro):
                    coro = self._coros.get(int(rcoro._id), None)
                    if isinstance(coro, Coro):
                        assert monitor._location != self._location
                        reply = self._monitor(monitor, coro)
                yield conn.send_msg(serialize(reply))
            elif req.name == 'exception':
                # synchronous message
                assert req.src is None
                assert req.dst == self._location
                reply = -1
                rcoro = req.kwargs.get('coro', None)
                if isinstance(rcoro, Coro):
                    coro = self._coros.get(int(rcoro._id), None)
                    if isinstance(coro, Coro):
                        exc = req.kwargs.get('exception', None)
                        if isinstance(exc, tuple):
                            reply = self._throw(coro, *exc)
                yield conn.send_msg(serialize(reply))
            elif req.name == 'ping':
                try:
                    req_peer = req.kwargs['peer']
                    if self._secret is None:
                        auth_code = None
                    else:
                        auth_code = hashlib.sha1(bytes(req.kwargs['signature'] + self._secret,
                                                       'ascii')).hexdigest()
                    assert req.kwargs['version'] == __version__
                except:
                    # logger.debug(traceback.format_exc())
                    break
                if req_peer == self._location:
                    break
                peer = _Peer.peers.get((req_peer.addr, req_peer.port), None)
                if peer and peer.auth == auth_code:
                    logger.debug('ignoring peer: %s' % (req_peer))
                    break
                pong = _NetRequest('pong',
                                   kwargs={'peer':self._location, 'signature':self._signature,
                                           'version':__version__}, dst=req_peer, auth=auth_code)
                sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                                   keyfile=self._keyfile, certfile=self._certfile)
                sock.settimeout(2)
                try:
                    yield sock.connect((req_peer.addr, req_peer.port))
                    yield sock.send_msg(serialize(pong))
                    reply = yield sock.recv_msg()
                    assert reply == b'ack'
                except:
                    logger.debug('ignoring peer %s', req_peer)
                    break
                finally:
                    sock.close()

                logger.debug('found asyncoro at %s' % req_peer)
                # relay ping to other asyncoro's running on same node
                peers = [(port, peer) for ((addr, port), peer) in _Peer.peers.items() \
                         if addr == self._location.addr and port != self._location.port]
                for port, peer in peers:
                    relay_req = _NetRequest('ping',
                                            kwargs={'peer':req_peer, 'version':__version__,
                                                    'signature':req.kwargs['signature']},
                                            dst=Location(self._location.addr, port), timeout=1)
                    _Peer.send_req(relay_req)

                if (req_peer.addr, req_peer.port) in _Peer.peers:
                    break
                peer = _Peer(req_peer, auth_code, self._keyfile, self._certfile)
                if (req_peer.addr, req_peer.port) in self._stream_peers or \
                       (req_peer.addr, 0) in self._stream_peers:
                    peer.stream = True

                # send pending (async) requests
                pending_reqs = [(i, copy.deepcopy(req)) for i, req in self._requests.items() \
                                if req.dst is None or req.dst == req_peer]
                for rid, pending_req in pending_reqs:
                    sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                                       keyfile=self._keyfile, certfile=self._certfile)
                    if pending_req.timeout:
                        sock.settimeout(pending_req.timeout)
                    try:
                        yield sock.connect((req_peer.addr, req_peer.port))
                        pending_req.auth = auth_code
                        yield sock.send_msg(serialize(pending_req))
                    except:
                        # logger.debug(traceback.format_exc())
                        pass
                    sock.close()
            elif req.name == 'pong':
                try:
                    req_peer = req.kwargs['peer']
                    assert req.kwargs['version'] == __version__
                    if self._secret is None:
                        auth_code = None
                    else:
                        auth_code = hashlib.sha1(bytes(req.kwargs['signature'] + self._secret,
                                                       'ascii')).hexdigest()
                    # assert req_peer == req.src
                    peer = _Peer.peers.get((req_peer.addr, req_peer.port), None)
                    if peer and peer.auth == auth_code:
                        logger.debug('ignoring peer: %s' % (req_peer))
                        yield conn.send_msg(b'nak')
                        break
                    yield conn.send_msg(b'ack')
                except:
                    logger.debug('ignoring peer: %s' % req_peer)
                    # logger.debug(traceback.format_exc())
                    break

                logger.debug('found asyncoro at %s' % req_peer)
                # relay ping to other asyncoro's running on same node
                peers = [(port, peer) for ((addr, port), peer) in _Peer.peers.items() \
                         if addr == self._location.addr and port != self._location.port]
                for port, peer in peers:
                    relay_req = _NetRequest('ping',
                                            kwargs={'peer':req_peer, 'version':__version__,
                                                    'signature':req.kwargs['signature']},
                                            dst=Location(self._location.addr, port), timeout=1)
                    _Peer.send_req(relay_req)

                if (req_peer.addr, req_peer.port) in _Peer.peers:
                    break
                peer = _Peer(req_peer, auth_code, self._keyfile, self._certfile)
                if (req_peer.addr, req_peer.port) in self._stream_peers or \
                       (req_peer.addr, 0) in self._stream_peers:
                    peer.stream = True

                # send pending (async) requests
                pending_reqs = [(i, copy.deepcopy(req)) for i, req in self._requests.items() \
                                if req.dst is None or req.dst == req_peer]
                for rid, pending_req in pending_reqs:
                    sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                                       keyfile=self._keyfile, certfile=self._certfile)
                    if pending_req.timeout:
                        sock.settimeout(pending_req.timeout)
                    try:
                        yield sock.connect((req_peer.addr, req_peer.port))
                        pending_req.auth = auth_code
                        yield sock.send_msg(serialize(pending_req))
                    except:
                        # logger.debug(traceback.format_exc())
                        pass
                    sock.close()
            elif req.name == 'unsubscribe':
                # synchronous message
                assert req.src is None
                assert req.dst == self._location
                reply = -1
                channel = self._rchannels.get(req.kwargs['name'], None)
                if channel is not None and channel._location == self._location:
                    rcoro = req.kwargs.get('coro', None)
                    if rcoro is not None:
                        subscriber = rcoro
                    else:
                        rchannel = req.kwargs.get('channel', None)
                        if rchannel is not None:
                            subscriber = rchannel
                    if subscriber is not None:
                        reply = yield channel.unsubscribe(subscriber)
                yield conn.send_msg(serialize(reply))
            elif req.name == 'locate_peer':
                if req.kwargs['name'] == self._name or req.dst == self._location:
                    if req.kwargs['name'] == self._name:
                        loc = self._location
                    elif req.dst == self._location:
                        loc = None
                    if req.src:
                        peer = _Peer.peers.get((req.src.addr, req.src.port), None)
                        if peer:
                            sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                                               keyfile=self._keyfile, certfile=self._certfile)
                            try:
                                yield sock.connect((req.src.addr, req.src.port))
                                req.auth = peer.auth
                                req.reply = loc
                                yield sock.send_msg(serialize(req))
                            except:
                                pass
                            sock.close()
                    else:
                        yield conn.send_msg(serialize(loc))
            elif req.name == 'send_file':
                # synchronous message
                assert req.src is None
                assert req.dst == self._location
                tgt = os.path.basename(req.kwargs['file'])
                dest_path = req.kwargs['dest_path']
                if isinstance(dest_path, str):
                    tgt = os.path.join(dest_path, tgt)
                tgt = os.path.abspath(os.path.join(self.dest_path_prefix, tgt))
                stat_buf = req.kwargs['stat_buf']
                resp = 0
                if self.max_file_size and stat_buf.st_size > self.max_file_size:
                    logger.warning('file "%s" too big (%s) - must be smaller than %s',
                                   req.kwargs['file'], stat_buf.st_size, self.max_file_size)
                    resp = -1
                elif not tgt.startswith(self.dest_path_prefix):
                    resp = -1
                elif os.path.isfile(tgt):
                    sbuf = os.stat(tgt)
                    if abs(stat_buf.st_mtime - sbuf.st_mtime) <= 1 and \
                           stat_buf.st_size == sbuf.st_size and \
                           stat.S_IMODE(stat_buf.st_mode) == stat.S_IMODE(sbuf.st_mode):
                        resp = 1
                    elif not req.kwargs['overwrite']:
                        resp = sbuf

                if resp == 0:
                    try:
                        if not os.path.isdir(os.path.dirname(tgt)):
                            os.makedirs(os.path.dirname(tgt))
                        fd = open(tgt, 'wb')
                    except:
                        logger.debug('failed to create "%s" : %s', tgt, traceback.format_exc())
                        resp = -1
                yield conn.send_msg(serialize(resp))
                if resp == 0:
                    n = 0
                    try:
                        while n < stat_buf.st_size:
                            data = yield conn.recvall(min(stat_buf.st_size-n, 10240000))
                            if not data:
                                break
                            fd.write(data)
                            n += len(data)
                    except:
                        logger.warning('copying file "%s" failed', tgt)
                    fd.close()
                    if n < stat_buf.st_size:
                        os.remove(tgt)
                        resp = -1
                    else:
                        resp = 0
                        logger.debug('saved file %s', tgt)
                        os.utime(tgt, (stat_buf.st_atime, stat_buf.st_mtime))
                        os.chmod(tgt, stat.S_IMODE(stat_buf.st_mode))
                    yield conn.send_msg(serialize(resp))
            elif req.name == 'del_file':
                # synchronous message
                assert req.src is None
                assert req.dst == self._location
                tgt = os.path.basename(req.kwargs['file'])
                dest_path = req.kwargs['dest_path']
                if isinstance(dest_path, str) and dest_path:
                    tgt = os.path.join(dest_path, tgt)
                tgt = os.path.join(self.dest_path_prefix, tgt)
                if tgt.startswith(self.dest_path_prefix) and os.path.isfile(tgt):
                    os.remove(tgt)
                    d = os.path.dirname(tgt)
                    try:
                        while d > self.dest_path_prefix and os.path.isdir(d):
                            os.rmdir(d)
                            d = os.path.dirname(d)
                    except:
                        # logger.debug(traceback.format_exc())
                        pass
                    reply = 0
                else:
                    reply = -1
                yield conn.send_msg(serialize(reply))
            elif req.name == 'terminate':
                # synchronous message
                assert req.src is None
                peer = req.kwargs.get('peer', None)
                if peer:
                    logger.debug('peer %s:%s terminated' % (peer.addr, peer.port))
                    _Peer.remove(peer)
                try:
                    yield conn.send_msg(serialize(b'ack'))
                except:
                    pass
                break
            else:
                logger.warning('invalid request "%s" ignored', req.name)
        conn.close()

    def _async_reply(self, req, peer, dst=None):
        """Internal use only.
        """
        if dst is None:
            dst = req.dst
        sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                           keyfile=self._keyfile, certfile=self._certfile)
        if req.timeout:
            sock.settimeout(req.timeout)
        try:
            yield sock.connect((dst.addr, dst.port))
            req.auth = peer.auth
            yield sock.send_msg(serialize(req))
        except socket.error as exc:
            logger.debug('could not send "%s" to %s', req.name, dst)
            if len(exc.args) == 1 and exc.args[0] == 'hangup':
                logger.warning('peer "%s" not reachable' % dst)
                # TODO: remove peer?
        except:
            logger.debug('could not send "%s" to %s', req.name, dst)
        sock.close()

    def _sync_reply(self, req, alarm_value=None):
        """Internal use only.
        """
        assert req.src is None
        req.event = Event()
        _Peer.send_req(req)
        if (yield req.event.wait(req.timeout)) is False:
            raise StopIteration(alarm_value)
        raise StopIteration(req.reply)

    def _register_channel(self, channel, name):
        """Internal use only.
        """
        if self._rchannels.get(name, None) is None:
            self._rchannels[name] = channel
            return 0
        else:
            logger.warning('channel "%s" is already registered', name)
            return -1

    def _unregister_channel(self, channel, name):
        """Internal use only.
        """
        if self._rchannels.get(name, None) is channel:
            self._rchannels.pop(name)
            return 0
        else:
            logger.warning('unregister of "%s" is invalid', name)
            return -1

    def _register_coro(self, coro, name):
        """Internal use only.
        """
        if self._rcoros.get(name, None) is None:
            self._rcoros[name] = coro
            return 0
        else:
            logger.warning('coro "%s" is already registered', name)
            return -1

    def _unregister_coro(self, coro, name):
        """Internal use only.
        """
        if self._rcoros.get(name, None) is coro:
            self._rcoros.pop(name)
            return 0
        else:
            logger.warning('unregister of "%s" is invalid', name)
            return -1

asyncoro._NetRequest = _NetRequest
asyncoro._Peer = _Peer
asyncoro.AsynCoro = AsynCoro