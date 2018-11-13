#!/user/env python3
# -*- coding: utf-8 -*-

import json
import bjson
import logging
import random
import socket
import time
import zlib
import selectors
from threading import Thread, Lock, Event
from nem_ed25519.base import Encryption
from .tool.traffic import Traffic
from .tool.utils import AESCipher, QueueSystem
from .config import C, V, Debug, PeerToPeerError
from .user import User

# constant
SERVER_SIDE = 'Server'
CLIENT_SIDE = 'Client'

listen_sel = selectors.DefaultSelector()


class Core:
    f_stop = False
    f_finish = False
    f_running = False

    def __init__(self, host=None, listen=15, buffsize=4096):
        assert V.DATA_PATH is not None, 'Setup p2p params before CoreClass init.'
        self.start_time = int(time.time())
        self.number = 0
        self.user = list()
        self.lock = Lock()
        self.host = host  # local=>'localhost', 'global'=>None
        self.ecc = Encryption()
        self.ecc.secret_key()
        self.ecc.public_key()
        self.core_que = QueueSystem(maxsize=listen*100)
        self.listen = listen
        self.buffsize = buffsize
        self.traffic = Traffic()
        self._ping = Event()
        self._ping.set()
        self.udp_ipv4_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_ipv6_sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)

    def close(self):
        if not self.f_running:
            raise PeerToPeerError('Core is not running.')
        self.traffic.close()
        for user in self.user.copy():
            self.remove_connection(user, 'Manually closing.')
        listen_sel.close()
        self.f_stop = True

    def ping(self, user: User, f_udp=False):
        try:
            self._ping.wait(10)
            self._ping.clear()
            self.send_msg_body(msg_body=b'Ping', user=user, f_udp=f_udp, f_pro_force=True)
            r = self._ping.wait(5)
            self._ping.set()
            return r
        except Exception as e:
            logging.debug("Failed ping by {} udp={}".format(e, f_udp))
            self._ping.set()
            return False

    def start(self, s_family=socket.AF_UNSPEC):
        def tcp_server_listen(server_sock, mask):
            try:
                sock, host_port = server_sock.accept()
                Thread(target=self._initial_connection_check,
                       args=(sock, host_port), daemon=True).start()
                logging.info("Server accept from {}".format(host_port))
            except OSError as e:
                logging.debug("OSError {}".format(e))
            except Exception as e:
                logging.debug(e, exc_info=Debug.P_EXCEPTION)

        def udp_server_listen(server_sock, mask):
            try:
                msg, address = server_sock.recvfrom(8192)
                msg_len = msg[0]
                msg_name, msg_body = msg[1:msg_len+1], msg[msg_len+1:]
                user = self.name2user(msg_name.decode())
                if user is None or not user.p2p_udp_accept:
                    return
                self.traffic.put_traffic_down(msg_body)
                msg_body = AESCipher.decrypt(key=user.aeskey, enc=msg_body)
                if msg_body == b'Ping':
                    logging.debug("Get udp accept from {}".format(user))
                    self.send_msg_body(msg_body=b'Pong', user=user)
                else:
                    logging.debug("Get udp packet from {}".format(user))
                    self.core_que.broadcast((user, msg_body))
            except OSError as e:
                logging.debug("OSError {}".format(e))
            except Exception as e:
                logging.debug(e, exc_info=Debug.P_EXCEPTION)

        def create_tcp_server_socks():
            for res in socket.getaddrinfo(self.host, V.P2P_PORT, s_family, socket.SOCK_STREAM, 0, socket.AI_PASSIVE):
                af, sock_type, proto, canon_name, sa = res
                try:
                    sock = socket.socket(af, sock_type, proto)
                except OSError as e:
                    logging.debug("Failed tcp socket.socket {}".format(sa))
                    continue
                try:
                    sock.bind(sa)
                    sock.listen(self.listen)
                    sock.setblocking(False)
                except OSError as e:
                    sock.close()
                    logging.debug("Failed tcp bind or listen {}".format(sa))
                    continue
                if af == socket.AF_INET or af == socket.AF_INET6:
                    listen_sel.register(sock, selectors.EVENT_READ, tcp_server_listen)
                    logging.info("New tcp server {} {}".format("IPV4" if sock.family == 2 else "IPV6", sa))
                else:
                    logging.warning("Not found socket type {}".format(af))
            if len(listen_sel.get_map()) == 0:
                logging.error('could not open tcp sockets')
                V.P2P_ACCEPT = False

        def create_udp_server_socks():
            before_num = len(listen_sel.get_map())
            for res in socket.getaddrinfo(self.host, V.P2P_PORT, s_family, socket.SOCK_DGRAM, 0, socket.AI_PASSIVE):
                af, sock_type, proto, canon_name, sa = res
                try:
                    sock = socket.socket(af, sock_type, proto)
                except OSError as e:
                    logging.debug("Failed udp socket.socket {}".format(sa))
                    continue
                try:
                    sock.bind(sa)
                    sock.setblocking(False)
                except OSError as e:
                    sock.close()
                    logging.debug("Failed udp bind {}".format(sa))
                    continue
                if af == socket.AF_INET or af == socket.AF_INET6:
                    listen_sel.register(sock, selectors.EVENT_READ, udp_server_listen)
                    logging.info("New udp server {} {}".format("IPV4" if sock.family == 2 else "IPV6", sa))
                else:
                    logging.warning("Not found socket type {}".format(af))
            if len(listen_sel.get_map()) == before_num:
                logging.error('could not open udp sockets')
                V.P2P_UDP_ACCEPT = False

        def sock_listen_loop():
            while not self.f_stop:
                try:
                    while len(listen_sel.get_map()) == 0:
                        time.sleep(0.5)
                    events = listen_sel.select()
                    for key, mask in events:
                        callback = key.data
                        callback(key.fileobj, mask)
                except BaseException as e:
                    logging.error(e)
                    time.sleep(3)

        assert s_family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNSPEC)
        self.traffic.start()
        # Pooling connection
        if not V.P2P_ACCEPT:
            logging.info('You set p2p accept flag False.')
        else:
            # create server ipv4/ipv6
            create_tcp_server_socks()
            create_udp_server_socks()
            Thread(target=sock_listen_loop, name='Listen', daemon=True).start()
        self.f_running = True

    def get_server_header(self):
        return {
            'name': V.SERVER_NAME,
            'client_ver': V.CLIENT_VER,
            'network_ver': V.NETWORK_VER,
            'p2p_accept': V.P2P_ACCEPT,
            'p2p_udp_accept': V.P2P_UDP_ACCEPT,
            'p2p_port': V.P2P_PORT,
            'start_time': self.start_time}

    def create_connection(self, host, port):
        sock = host_port = None
        try:
            for res in socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM):
                af, socktype, proto, canonname, host_port = res
                try:
                    sock = socket.socket(af, socktype, proto)
                except OSError:
                    continue
                sock.settimeout(10)
                # Connection
                try:
                    sock.connect(host_port)
                    break
                except OSError:
                    sock.close()
                    continue
            else:
                # create no connection
                return False
            logging.debug("Success connection create to {}".format(host_port))
            # ヘッダーを送る
            send = json.dumps(self.get_server_header()).encode()
            sock.sendall(send)
            self.traffic.put_traffic_up(send)
            # 公開鍵を受取る
            receive = sock.recv(self.buffsize)
            self.traffic.put_traffic_down(receive)
            public_key = json.loads(receive.decode())['public-key']
            # 公開鍵を送る
            send = json.dumps({'public-key': self.ecc.pk}).encode()
            sock.sendall(send)
            self.traffic.put_traffic_up(send)
            # AESKEYとヘッダーを取得し復号化する
            receive = sock.recv(self.buffsize)
            self.traffic.put_traffic_down(receive)
            data = json.loads(self.ecc.decrypt(sender_pk=public_key, enc=receive).decode())
            aeskey, header = data['aes-key'], data['header']
            logging.debug("Success ase-key receive {}".format(host_port))
            # ユーザーを作成する
            with self.lock:
                new_user = User(self.number, sock, host_port, aeskey, C.T_CLIENT)
                new_user.deserialize(header)
                # headerのチェック
                if new_user.network_ver != V.NETWORK_VER:
                    raise PeerToPeerError('Don\'t same network version [{}!={}]'
                                          .format(new_user.network_ver, V.NETWORK_VER))
                self.number += 1
            # Acceptシグナルを送る
            encrypted = AESCipher.encrypt(new_user.aeskey, b'accept')
            sock.sendall(encrypted)
            self.traffic.put_traffic_up(encrypted)

            logging.info("New connection to \"{}\" {}".format(new_user.name, new_user.get_host_port()))
            Thread(target=self._receive_msg,
                   name='C:' + new_user.name, args=(new_user,), daemon=True).start()

            c = 20
            while new_user not in self.user and c > 0:
                time.sleep(1)
                c -= 1
            self.is_reachable(new_user)
            if c == 0:
                return False
            else:
                return True
        except json.JSONDecodeError:
            error = "Json decode error."
        except PeerToPeerError as e:
            error = "NewConnectionError {} {}".format(host_port, e)
        except ConnectionRefusedError as e:
            error = "ConnectionRefusedError {} {}".format(host_port, e)
        except Exception as e:
            error = "NewConnectionError {} {}".format(host_port, e)

        # close socket
        logging.debug(error)
        try: sock.sendall(error.encode())
        except: pass
        try: sock.shutdown(socket.SHUT_RDWR)
        except: pass
        try: sock.close()
        except: pass
        return False

    def remove_connection(self, user, reason=None):
        if user is None:
            return False
        try:
            if reason:
                user.send(b'1111'+str(reason).encode())
        except:
            pass
        user.close()
        if user in self.user.copy():
            with self.lock:
                self.user.remove(user)
            logging.debug("remove connection to {} by \"{}\"".format(user.name, reason))
            return True
        else:
            logging.debug("failed remove connection by \"{}\", not found {}".format(reason, user.name))
            return False

    def send_msg_body(self, msg_body, user=None, status=200, f_udp=False, f_pro_force=False):
        # StatusCode: https://ja.wikipedia.org/wiki/HTTPステータスコード
        assert type(msg_body) == bytes, 'msg_body is bytes'
        assert 200 <= status < 600, 'Not found status code {}'.format(status)

        # get client
        if len(self.user) == 0:
            raise ConnectionError('client connection is zero.')
        elif len(msg_body) > C.MAX_RECEIVE_SIZE + 5000:
            error = 'Max message size is {}kb (You try {}Kb)'.format(
                round(C.MAX_RECEIVE_SIZE/1000000, 3), round(len(msg_body)/1000000, 3))
            self.send_msg_body(msg_body=bjson.dumps(error), user=user, status=500)
            raise ConnectionRefusedError(error)
        elif user is None:
            user = random.choice(self.user)

        # send message
        if f_udp and f_pro_force:
            self._udp_body(msg_body, user)
        elif f_udp and user.p2p_udp_accept and len(msg_body) < 1400:
            self._udp_body(msg_body, user)
        else:
            msg_body = zlib.compress(msg_body)
            msg_body = AESCipher.encrypt(key=user.aeskey, raw=msg_body)
            msg_len = len(msg_body).to_bytes(4, 'big')
            send_data = msg_len + msg_body
            user.send(send_data)
            self.traffic.put_traffic_up(send_data)
        # logging.debug("Send {}Kb to '{}'".format(len(msg_len+msg_body) / 1000, user.name))
        return user

    def _udp_body(self, msg_body, user):
        name_len = len(V.SERVER_NAME.encode()).to_bytes(1, 'big')
        msg_body = AESCipher.encrypt(key=user.aeskey, raw=msg_body)
        send_data = name_len + V.SERVER_NAME.encode() + msg_body
        host_port = user.get_host_port()
        if len(host_port) == 2:
            self.udp_ipv4_sock.sendto(send_data, host_port)
        else:
            self.udp_ipv6_sock.sendto(send_data, host_port)
        self.traffic.put_traffic_up(send_data)

    def _initial_connection_check(self, sock, host_port):
        sock.settimeout(10)
        try:
            # ヘッダーを受取る
            received = sock.recv(self.buffsize)
            if len(received) == 0:
                raise ConnectionAbortedError('zero msg, connection closed.')
            self.traffic.put_traffic_down(received)
            header = json.loads(received.decode())
            with self.lock:
                new_user = User(self.number, sock, host_port,
                                aeskey=AESCipher.create_key(), sock_type=C.T_SERVER)
                self.number += 1
            new_user.deserialize(header)
            if new_user.name == V.SERVER_NAME:
                raise ConnectionAbortedError('Same origin connection.')
            # こちらの公開鍵を送る
            send = json.dumps({'public-key': self.ecc.pk}).encode()
            sock.sendall(send)
            self.traffic.put_traffic_up(send)
            # 公開鍵を取得する
            receive = new_user.sock.recv(self.buffsize)
            self.traffic.put_traffic_down(receive)
            if len(receive) == 0:
                raise ConnectionAbortedError('received msg is zero.')
            public_key = json.loads(receive.decode())['public-key']
            # AESKEYとHeaderを暗号化して送る
            encrypted = self.ecc.encrypt(recipient_pk=public_key, msg=json.dumps(
                {'aes-key': new_user.aeskey, 'header': self.get_server_header()}).encode(), encode='raw')
            new_user.send(encrypted)
            self.traffic.put_traffic_up(encrypted)
            # Accept信号を受け取る
            encrypted = new_user.sock.recv(self.buffsize)
            self.traffic.put_traffic_down(encrypted)
            receive = AESCipher.decrypt(new_user.aeskey, encrypted)
            if receive != b'accept':
                raise ConnectionAbortedError('Not accept signal.')
            # Accept connection
            logging.info("New connection from \"{}\" {}".format(new_user.name, new_user.get_host_port()))
            Thread(target=self._receive_msg,
                   name='S:' + new_user.name, args=(new_user,), daemon=True).start()
            # Port accept check
            time.sleep(10)
            if new_user in self.user:
                self.is_reachable(new_user)
            return
        except ConnectionAbortedError as e:
            error = "ConnectionAbortedError, {}".format(e)
        except json.decoder.JSONDecodeError as e:
            error = "JSONDecodeError, {}".format(e)
        except socket.timeout:
            error = "socket.timeout"
        except Exception as e:
            error = "Exception as {}".format(e)
        # close socket
        error = "Close on initial check " + error
        logging.debug(error)
        try: sock.sendall(error.encode())
        except: pass
        try: sock.close()
        except: pass

    def _receive_msg(self, user):
        # Accept connection
        with self.lock:
            for check_user in self.user:
                if check_user.name != user.name:
                    continue
                if self.ping(check_user):
                    error = "Remove new connection {}, continue connect {}".format(user, check_user)
                    self.remove_connection(user, error)
                    logging.info(error)
                else:
                    error = "Same origin, Replace new connection {} => {}".format(check_user, user)
                    self.remove_connection(check_user, error)
                    logging.info(error)
            self.user.append(user)
        logging.info("Accept connection \"{}\"".format(user.name))

        # pooling
        msg_prefix = b''
        msg_len = 0
        msg_body = b''
        error = None
        try:
            while not self.f_stop:
                if len(msg_prefix) == 0:
                    user.sock.settimeout(3600)
                    first_msg = user.sock.recv(self.buffsize)
                    user.sock.settimeout(10)
                else:
                    first_msg, msg_prefix = msg_prefix, b''

                # Start receive message
                msg_len = int.from_bytes(first_msg[:4], 'big')
                msg_body = first_msg[4:]

                # Notice long message
                if Debug.F_LONG_MSG_INFO and msg_len != len(msg_body):
                    logging.debug("Receive long msg, len=%d, body=%d" % (msg_len, len(msg_body)))

                if msg_len == 0:
                    raise ConnectionAbortedError("1:Socket error, fall in loop.")
                elif len(msg_body) == 0:
                    raise ConnectionAbortedError("2:Socket error, fall in loop.")
                elif len(msg_body) >= msg_len:
                    msg_body, msg_prefix = msg_body[:msg_len], msg_body[msg_len:]
                    self.traffic.put_traffic_down(msg_body)
                    msg_body = AESCipher.decrypt(key=user.aeskey, enc=msg_body)
                    msg_body = zlib.decompress(msg_body)
                    if msg_body == b'Ping':
                        logging.debug("receive ping from {}".format(user.name))
                        self.send_msg_body(b'Pong', user)
                        continue
                    elif msg_body == b'Pong':
                        logging.debug("receive Pong from {}".format(user.name))
                        self._ping.set()
                        continue
                    else:
                        self.core_que.broadcast((user, msg_body))
                    continue

                # continue receiving message
                while True:
                    new_body = user.sock.recv(self.buffsize)
                    msg_body += new_body
                    if len(new_body) == 0:
                        raise ConnectionAbortedError("3:Socket error, fall in loop.")
                    elif len(msg_body) >= msg_len:
                        msg_body, msg_prefix = msg_body[:msg_len], msg_body[msg_len:]
                        self.traffic.put_traffic_down(msg_body)
                        msg_body = AESCipher.decrypt(key=user.aeskey, enc=msg_body)
                        msg_body = zlib.decompress(msg_body)
                        self.core_que.broadcast((user, msg_body))
                        break
                    elif len(msg_body) > C.MAX_RECEIVE_SIZE + 5000:
                        raise ConnectionAbortedError("Too many data! (MAX {}Kb)"
                                                     .format(C.MAX_RECEIVE_SIZE // 1000))
                    else:
                        continue

        except socket.timeout:
            error = "socket timeout {}".format(user.name)
            logging.debug(error)
        except ConnectionAbortedError as e:
            error = "ConnectionAbortedError :len={}, msg={} e={}".format(msg_len, msg_body, e)
        except ConnectionResetError:
            error = "ConnectionResetError by {}".format(user.name)
        except OSError as e:
            error = "OSError by {}, {}".format(user.name, e)
        except Exception as e:
            error = "BaseException by {}, {}".format(user.name, e)

        # raised exception on loop
        logging.debug(error)
        if not self.remove_connection(user, error):
            logging.debug("Failed remove user {}".format(user.name))

    def is_reachable(self, new_user):
        # Check connect to the user TCP/UDP port
        if new_user not in self.user:
            return
        f_tcp = True
        host_port = new_user.get_host_port()
        af = socket.AF_INET if len(host_port) == 2 else socket.AF_INET6
        try:
            sock = socket.socket(af, socket.SOCK_STREAM)
            r = sock.connect_ex(host_port)
            if r != 0:
                f_tcp = False
            sock.close()
        except OSError:
            f_tcp = False
        f_udp = self.ping(user=new_user, f_udp=True)
        # reflect user status
        if f_tcp is not new_user.p2p_accept:
            logging.debug("{} Update TCP accept status [{}>{}]"
                          .format(new_user, new_user.p2p_accept, f_tcp))
            new_user.p2p_accept = f_tcp
        if f_udp is not new_user.p2p_udp_accept:
            logging.debug("{} Update UDP accept status [{}>{}]"
                          .format(new_user, new_user.p2p_udp_accept, f_udp))
            new_user.p2p_udp_accept = f_udp

    def name2user(self, name):
        for user in self.user:
            if user.name == name:
                return user
        return None

    def host_port2user(self, host_port):
        for user in self.user:
            if host_port == user.get_host_port():
                return user
        return None
