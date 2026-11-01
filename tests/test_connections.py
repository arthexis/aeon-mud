"""Loopback integration tests for connection establishment and recovery."""
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


from aeon_mud import profiles, session, telnet, transport
from support import LocalServer



class ConnectionTests(unittest.TestCase):
    def test_socks5_fragmented_handshake_and_payload(self):
        def respond(peer, _index):
            self.assertEqual(transport.recv_exact(peer, 3), b"\x05\x01\x00")
            for chunk in (b"\x05", b"\x00"):
                peer.sendall(chunk)
            request = transport.recv_exact(peer, 4)
            self.assertEqual(request[:3], b"\x05\x01\x00")
            self.assertEqual(request[3], 3)  # hostname form
            length = transport.recv_exact(peer, 1)[0]
            hostname = transport.recv_exact(peer, length).decode()
            self.assertEqual(hostname, "mud.example")
            self.assertEqual(transport.recv_exact(peer, 2), b"\x0f\xa0")
            # Deliberately split response fields and the bound address.
            for part in (b"\x05", b"\x00\x00", b"\x01", b"\x7f", b"\x00", b"\x00\x01\x00", b"\x00"):
                peer.sendall(part)
            peer.sendall(b"hello")
        with LocalServer(respond) as srv:
            p = profiles.Profile(name="proxy", host="mud.example", port=4000,
                            socks5_host="127.0.0.1", socks5_port=srv.port)
            sock = transport.connect_socks5(p)
            try:
                self.assertEqual(transport.recv_exact(sock, 5), b"hello")
            finally:
                sock.close()

    def test_socks5_truncated_response_rejected(self):
        def respond(peer, _index):
            transport.recv_exact(peer, 3)
            peer.sendall(b"\x05")
        with LocalServer(respond) as srv:
            p = profiles.Profile(name="proxy", host="mud.example",
                            socks5_host="127.0.0.1", socks5_port=srv.port)
            with self.assertRaises(OSError):
                transport.connect_socks5(p)

    def test_direct_connection_validates_actual_peer(self):
        class FakeSocket:
            closed = False
            def getpeername(self):
                return ("8.8.8.8", 4000)
            def close(self):
                self.closed = True
            def setsockopt(self, *_args):
                pass
            def settimeout(self, *_args):
                pass

        sock = FakeSocket()
        p = profiles.Profile(name="direct", host="mud.example")
        with mock.patch.object(transport.socket, "create_connection", return_value=sock):
            with self.assertRaisesRegex(OSError, "private-address policy"):
                transport.make_transport(p, require_private_peer=True)
        self.assertTrue(sock.closed)

    def test_local_telnet_connect_and_reconnect(self):
        def respond(peer, _index):
            # The server requests NAWS before the client may send a size.
            peer.sendall(bytes([telnet.IAC, telnet.DO, telnet.TELNET_NAWS]))
            response = peer.recv(256)
            if bytes([telnet.IAC, telnet.WILL, telnet.TELNET_NAWS]) not in response:
                raise AssertionError("Client did not negotiate NAWS")
            for chunk in (b"log", b"in: "):
                peer.sendall(chunk)
            # Keep the listener responsive for the next connection.
            threading.Event().wait(0.01)

        with tempfile.TemporaryDirectory() as tmp, LocalServer(respond, count=2) as srv:
            p = profiles.Profile(name="integration", host="127.0.0.1", port=srv.port,
                            allow_plaintext=True, expected_banner="login",
                            sanity_timeout=0.05, reconnect_initial_delay=0.0)
            with mock.patch.object(mud, "known_hosts_path", return_value=Path(tmp) / "hosts.json"):
                transport, codec, banner = session.open_session_transport(p, require_private_peer=True)
                self.assertIn("login:", banner)
                session.run_sanity_checks(p, banner, False, False, interactive=False)

                class Ui:
                    def __init__(self):
                        self.status = ""
                        self.connected = True
                    def set_connected(self, value):
                        self.connected = value
                    def set_status(self, value):
                        self.status = value
                    def append_output(self, _value):
                        pass

                class Transcript:
                    def write(self, *_args):
                        pass

                ui = Ui()
                controller = session.SessionController(p, transport, codec, Transcript(), ui,
                                                   None, private_peer_required=True)
                controller.mark_disconnected(transport, "test disconnect")
                try:
                    self.assertTrue(session.reconnect_session(threading.Event(), controller,
                                                          Transcript(), "test disconnect"))
                    self.assertTrue(ui.connected)
                    self.assertIsNotNone(controller.current_connection())
                finally:
                    controller.close()


if __name__ == "__main__":
    unittest.main()
