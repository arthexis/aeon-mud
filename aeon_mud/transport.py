"""Socket, SOCKS5 and subprocess transports for Telnet sessions."""
from __future__ import annotations
import ipaddress
import os
import socket
import struct
import subprocess
from .profiles import Profile

class Transport:
    def sendall(self, data: bytes) -> None:
        raise NotImplementedError

    def recv(self, size: int) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class SocketTransport(Transport):
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock

    def sendall(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv(self, size: int) -> bytes:
        return self.sock.recv(size)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class ProcessTransport(Transport):
    def __init__(self, command: list[str]) -> None:
        if not command:
            raise ValueError("proxy_command cannot be empty")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    def sendall(self, data: bytes) -> None:
        if not self.process.stdin:
            raise OSError("proxy command stdin is closed")
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def recv(self, size: int) -> bytes:
        if not self.process.stdout:
            return b""
        return os.read(self.process.stdout.fileno(), size)

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        finally:
            # Popen does not close PIPE descriptors simply because its child
            # exited. Close both parent ends even on timeout/error.
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None and not stream.closed:
                    stream.close()


class ConnectionUnavailable(OSError):
    pass


class ConnectionChanged(OSError):
    pass


def configure_tcp_socket(sock: socket.socket, keepalive_interval: float) -> None:
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass
    if hasattr(socket, "TCP_NODELAY"):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
    if keepalive_interval <= 0:
        return
    idle_seconds = max(10, int(keepalive_interval))
    interval_seconds = max(5, min(idle_seconds, idle_seconds // 3 or 5))
    if os.name == "nt" and hasattr(socket, "SIO_KEEPALIVE_VALS"):
        try:
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle_seconds * 1000, interval_seconds * 1000))
        except OSError:
            pass
        return
    for option_name, value in (
        ("TCP_KEEPIDLE", idle_seconds),
        ("TCP_KEEPINTVL", interval_seconds),
        ("TCP_KEEPCNT", 3),
    ):
        if hasattr(socket, option_name):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, option_name), value)
            except OSError:
                pass


def recv_exact(sock: socket.socket, count: int) -> bytes:
    """Receive precisely count bytes or reject a truncated protocol frame."""
    result = bytearray()
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise OSError("Connection closed during SOCKS5 handshake")
        result.extend(chunk)
    return bytes(result)


def connect_socks5(profile: Profile) -> socket.socket:
    sock = socket.create_connection(
        (profile.socks5_host, profile.socks5_port), timeout=profile.connect_timeout
    )
    try:
        configure_tcp_socket(sock, profile.keepalive_interval)
        sock.sendall(b"\x05\x01\x00")
        if recv_exact(sock, 2) != b"\x05\x00":
            raise OSError("SOCKS5 proxy does not allow no-auth connections")

        try:
            ip = ipaddress.ip_address(profile.host)
        except ValueError:
            host_bytes = profile.host.encode("idna")
            if len(host_bytes) > 255:
                raise OSError("SOCKS5 destination hostname is too long")
            address = bytes([3, len(host_bytes)]) + host_bytes
        else:
            address = (b"\x01" if ip.version == 4 else b"\x04") + ip.packed

        sock.sendall(b"\x05\x01\x00" + address + struct.pack("!H", profile.port))
        header = recv_exact(sock, 4)
        if header[0] != 5 or header[2] != 0:
            raise OSError(f"Malformed SOCKS5 response: {header!r}")
        if header[1] != 0:
            raise OSError(f"SOCKS5 connect failed with status {header[1]}")
        atyp = header[3]
        if atyp == 1:
            length = 4
        elif atyp == 3:
            length = recv_exact(sock, 1)[0]
            if not length:
                raise OSError("SOCKS5 returned empty bound hostname")
        elif atyp == 4:
            length = 16
        else:
            raise OSError("SOCKS5 returned unknown address type")
        recv_exact(sock, length + 2)
        return sock
    except BaseException:
        sock.close()
        raise


def is_private_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def make_transport(profile: Profile, *, require_private_peer: bool = False) -> Transport:
    if profile.proxy_command:
        return ProcessTransport(profile.proxy_command)
    if profile.socks5_host:
        sock = connect_socks5(profile)
    else:
        sock = socket.create_connection((profile.host, profile.port), timeout=profile.connect_timeout)
        configure_tcp_socket(sock, profile.keepalive_interval)
    try:
        if require_private_peer and not is_private_address(sock.getpeername()[0]):
            raise OSError("Connected peer violates the private-address policy")
        sock.settimeout(None)
        return SocketTransport(sock)
    except BaseException:
        sock.close()
        raise


def connection_target_label(profile: Profile) -> str:
    if profile.proxy_command:
        return "proxy command: " + " ".join(profile.proxy_command)
    if profile.socks5_host:
        return f"{profile.host}:{profile.port} via SOCKS5 {profile.socks5_host}:{profile.socks5_port}"
    return f"{profile.host}:{profile.port}"


def is_private_target(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses: set[str] = {item[4][0] for item in infos}
    if not addresses:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return False
    return True


