from __future__ import annotations

from collections.abc import Callable

from PySide6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstance:
    def __init__(self, name: str, on_activate: Callable[[], None]) -> None:
        self._name = name
        self._on_activate = on_activate
        self._server = QLocalServer()
        self._activation_socket: QLocalSocket | None = None
        self._connections: set[QLocalSocket] = set()
        self._buffers: dict[QLocalSocket, bytearray] = {}
        self._server.newConnection.connect(self._receive)

    def acquire(self) -> bool:
        probe = QLocalSocket()
        probe.connectToServer(self._name)
        if probe.waitForConnected(250):
            # Keep the client socket alive until the primary instance has had a
            # chance to process the Qt event.  Destroying it here can discard
            # the activation payload when both instances share one event loop
            # (as they do in the behavior test).
            self._activation_socket = probe
            probe.write(b"show")
            probe.flush()
            probe.waitForBytesWritten(250)
            return False
        QLocalServer.removeServer(self._name)
        return self._server.listen(self._name)

    def close(self) -> None:
        if self._activation_socket is not None:
            self._activation_socket.disconnectFromServer()
            self._activation_socket = None
        for socket in tuple(self._connections):
            socket.disconnectFromServer()
        self._server.close()

    def _receive(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            self._connections.add(socket)
            self._buffers[socket] = bytearray()
            socket.readyRead.connect(lambda current=socket: self._read(current))
            socket.disconnected.connect(
                lambda current=socket: self._disconnected(current)
            )
            if socket.bytesAvailable() > 0:
                self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        buffer = self._buffers.get(socket)
        if buffer is None:
            return
        buffer.extend(socket.readAll().data())
        if b"show" in buffer:
            buffer.clear()
            self._on_activate()
            socket.disconnectFromServer()

    def _disconnected(self, socket: QLocalSocket) -> None:
        if socket.bytesAvailable() > 0:
            self._read(socket)
        self._connections.discard(socket)
        self._buffers.pop(socket, None)
        socket.deleteLater()
