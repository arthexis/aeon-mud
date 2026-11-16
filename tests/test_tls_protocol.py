"""Hermetic TLS protocol choice and downgrade-safety tests."""
import ssl
import unittest
from unittest import mock

from aeon_mud.profiles import Profile
from aeon_mud import cli, transport


class TLSProtocolTests(unittest.TestCase):
    def test_auto_is_default_for_new_and_legacy_saved_profiles(self):
        self.assertEqual(Profile("new", "mud.example.org").protocol, "auto")
        legacy = Profile.from_dict("legacy", {"host": "mud.example.org", "allow_plaintext": True})
        self.assertEqual(legacy.protocol, "auto")
        self.assertTrue(legacy.allow_plaintext)
        self.assertEqual(legacy.to_stored_dict()["protocol"], "auto")

    def test_cli_defaults_and_explicit_overrides(self):
        parser = cli.build_parser()
        connect = parser.parse_args(["connect", "--host", "mud.example.org"])
        self.assertIsNone(connect.protocol)
        profile = cli.apply_connect_overrides(Profile("new", "mud.example.org"), connect)
        self.assertEqual(profile.protocol, "auto")
        add = parser.parse_args(["profile", "add", "test", "--host", "mud.example.org"])
        self.assertEqual(add.protocol, "auto")
        explicit = parser.parse_args(["connect", "--host", "mud.example.org", "--protocol", "plain"])
        self.assertEqual(cli.apply_connect_overrides(Profile("new", "mud.example.org"), explicit).protocol, "plain")

    def test_auto_default_requires_consent_for_plaintext_retry(self):
        p = Profile("new", "mud.example.org")
        self.assertFalse(p.allow_plaintext)
        raw = mock.Mock()
        context = mock.Mock()
        error = ssl.SSLError("wrong version")
        error.reason = "WRONG_VERSION_NUMBER"
        context.wrap_socket.side_effect = error
        with mock.patch.object(transport, "_open_tcp", return_value=raw) as connect, mock.patch.object(
            transport.ssl, "create_default_context", return_value=context
        ):
            with self.assertRaises(ssl.SSLError):
                transport.make_transport(p)
        connect.assert_called_once()

    def test_tls_success_never_prompts_for_plaintext(self):
        p = Profile("public", "mud.example.org", allow_plaintext=True)
        raw = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.return_value = mock.Mock()
        authorize = mock.Mock(return_value=False)
        with mock.patch.object(transport, "_open_tcp", return_value=raw), mock.patch.object(
            transport.ssl, "create_default_context", return_value=context
        ):
            connection = transport.make_transport(p, authorize_plaintext=authorize)
        self.assertEqual(connection.protocol, "tls")
        authorize.assert_not_called()

    def test_auto_fallback_requests_consent_only_after_failed_tls(self):
        p = Profile("public", "mud.example.org", allow_plaintext=True)
        raw = mock.Mock()
        context = mock.Mock()
        error = ssl.SSLError("wrong version")
        error.reason = "WRONG_VERSION_NUMBER"
        context.wrap_socket.side_effect = error
        authorize = mock.Mock(return_value=False)
        with mock.patch.object(transport, "_open_tcp", return_value=raw) as connect, mock.patch.object(
            transport.ssl, "create_default_context", return_value=context
        ):
            with self.assertRaisesRegex(OSError, "not authorized"):
                transport.make_transport(p, authorize_plaintext=authorize)
        authorize.assert_called_once()
        connect.assert_called_once()

    def test_login_credentials_not_requested_before_transport(self):
        from aeon_mud import session
        args = cli.build_parser().parse_args([
            "connect", "--host", "127.0.0.1", "--port", "4000",
            "--login", "test_character",
        ])
        profile = Profile("test", "127.0.0.1")
        with mock.patch.object(session, "set_connection_tab_title"), mock.patch.object(
            session, "is_private_target", return_value=True
        ), mock.patch.object(
            session, "open_session_transport", side_effect=OSError("TLS unavailable")
        ), mock.patch.object(session.getpass, "getpass") as password:
            with self.assertRaisesRegex(SystemExit, "TLS unavailable"):
                session.session_loop(profile, args)
        password.assert_not_called()

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
        with self.assertRaisesRegex(OSError, "proxy_command"):
            transport.make_transport(p)

    def test_legacy_proxy_command_keeps_authorized_plaintext(self):
        p = Profile.from_dict("legacy", {
            "host": "mud.example.org", "allow_plaintext": True,
            "proxy_command": ["ssh", "-W", "target", "host"],
        })
        with mock.patch.object(transport, "ProcessTransport") as process:
            self.assertIs(transport.make_transport(p), process.return_value)
        process.assert_called_once_with(p.proxy_command)

    def test_proxy_command_without_plaintext_permission_fails_closed(self):
        p = Profile("blocked", "mud.example.org", proxy_command=["ssh", "-W", "target", "host"])
        with mock.patch.object(transport, "ProcessTransport") as process:
            with self.assertRaisesRegex(OSError, "allow-plaintext"):
                transport.make_transport(p)
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
