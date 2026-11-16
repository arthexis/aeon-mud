"""Hermetic TLS protocol choice and downgrade-safety tests."""
import ssl
import unittest
from unittest import mock

from aeon_mud.profiles import Profile
from aeon_mud import transport


class TLSProtocolTests(unittest.TestCase):
    def test_protocol_validation(self):
        with self.assertRaisesRegex(ValueError, "protocol"):
            Profile("invalid", "example.org", protocol="starttls")

    def test_tls_only_verifies_hostname(self):
        p = Profile("tls", "mud.example.org", protocol="tls")
        raw = mock.Mock()
        wrapped = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.return_value = wrapped
        with mock.patch.object(transport, "_open_tcp", return_value=raw), mock.patch.object(
            transport.ssl, "create_default_context", return_value=context
        ):
            result = transport.make_transport(p)
        self.assertIs(result.sock, wrapped)
        context.wrap_socket.assert_called_once_with(raw, server_hostname=p.host)

    def test_auto_without_consent_never_falls_back(self):
        p = Profile("auto", "mud.example.org", protocol="auto")
        raw = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.side_effect = ssl.SSLError("not TLS")
        with mock.patch.object(transport, "_open_tcp", return_value=raw) as connect, mock.patch.object(
            transport.ssl, "create_default_context", return_value=context
        ):
            with self.assertRaises(ssl.SSLError):
                transport.make_transport(p)
        connect.assert_called_once()
        raw.close.assert_called_once()

    def test_auto_certificate_failure_never_downgrades(self):
        p = Profile("auto", "mud.example.org", protocol="auto", allow_plaintext=True)
        raw = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("untrusted")
        with mock.patch.object(transport, "_open_tcp", return_value=raw) as connect, mock.patch.object(
            transport.ssl, "create_default_context", return_value=context
        ):
            with self.assertRaises(ssl.SSLCertVerificationError):
                transport.make_transport(p)
        connect.assert_called_once()

    def test_auto_non_tls_retries_fresh_socket_with_consent(self):
        p = Profile("auto", "mud.example.org", protocol="auto", allow_plaintext=True)
        first, second = mock.Mock(), mock.Mock()
        context = mock.Mock()
        error = ssl.SSLError("wrong version")
        error.reason = "WRONG_VERSION_NUMBER"
        context.wrap_socket.side_effect = error
        with mock.patch.object(transport, "_open_tcp", side_effect=[first, second]) as connect, mock.patch.object(
            transport.ssl, "create_default_context", return_value=context
        ):
            result = transport.make_transport(p)
        self.assertEqual(connect.call_count, 2)
        first.close.assert_called_once()
        self.assertIs(result.sock, second)

    def test_proxy_command_tls_is_rejected(self):
        p = Profile("tls", "mud.example.org", protocol="tls", proxy_command=["ssh", "-W", "target", "host"])
        with self.assertRaisesRegex(ValueError, "proxy_command"):
            transport.make_transport(p)


if __name__ == "__main__":
    unittest.main()
