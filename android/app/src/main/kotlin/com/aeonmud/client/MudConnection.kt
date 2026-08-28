package com.aeonmud.client

import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketException
import java.net.SocketTimeoutException

sealed class MudEvent {
    data class Output(val text: String) : MudEvent()
    data class Status(val text: String) : MudEvent()
    data class Closed(val text: String) : MudEvent()
}

class MudConnection(
    private val host: String,
    private val port: Int,
    private val listener: (MudEvent) -> Unit,
) {
    @Volatile
    private var running = false

    @Volatile
    private var socket: Socket? = null

    private val writeLock = Any()
    private val codec = TelnetCodec("ANSI")
    private var worker: Thread? = null

    fun start() {
        if (running) return
        running = true
        worker = Thread(::run, "aeon-mud-android-receiver").also {
            it.isDaemon = true
            it.start()
        }
    }

    fun disconnect() {
        running = false
        closeSocket()
        worker?.interrupt()
    }

    fun sendLine(line: String) {
        if (line.isBlank()) return
        Thread({
            try {
                for (buffer in splitOutboundBuffers(line)) {
                    if (!running) break
                    writeRaw((buffer + "\r\n").toByteArray(Charsets.UTF_8))
                    Thread.sleep(60)
                }
            } catch (exc: Exception) {
                listener(MudEvent.Status("Send failed: ${exc.message ?: exc.javaClass.simpleName}"))
            }
        }, "aeon-mud-android-sender").start()
    }

    private fun run() {
        var closeReason = "Disconnected."
        try {
            val nextSocket = Socket()
            nextSocket.tcpNoDelay = true
            nextSocket.keepAlive = false
            nextSocket.soTimeout = 1000
            nextSocket.connect(InetSocketAddress(host, port), 10_000)
            socket = nextSocket
            writeRaw(codec.setWindowSize(80, 24))
            listener(MudEvent.Status("Connected to $host:$port"))
            receiveLoop()
        } catch (exc: Exception) {
            closeReason = exc.message ?: exc.javaClass.simpleName
            if (running) listener(MudEvent.Status("Connection closed: $closeReason"))
        } finally {
            running = false
            closeSocket()
            listener(MudEvent.Closed(closeReason))
        }
    }

    private fun receiveLoop() {
        val buffer = ByteArray(4096)
        while (running) {
            val active = socket ?: throw IOException("not connected")
            try {
                val read = active.getInputStream().read(buffer)
                if (read < 0) throw IOException("remote closed the connection")
                val frame = codec.feed(buffer.copyOf(read))
                if (frame.response.isNotEmpty()) writeRaw(frame.response)
                if (frame.output.isNotEmpty()) {
                    listener(MudEvent.Output(TerminalText.toDisplay(String(frame.output, Charsets.UTF_8))))
                }
            } catch (_: SocketTimeoutException) {
                continue
            }
        }
    }

    private fun writeRaw(bytes: ByteArray) {
        synchronized(writeLock) {
            val active = socket ?: throw IOException("not connected")
            active.getOutputStream().write(bytes)
            active.getOutputStream().flush()
        }
    }

    private fun closeSocket() {
        val active = socket
        socket = null
        if (active != null) {
            try {
                active.close()
            } catch (_: SocketException) {
            }
        }
    }
}

object TerminalText {
    private val csi = Regex("\u001B\\[[0-?]*[ -/]*[@-~]")
    private val osc = Regex("\u001B\\].*?(?:\u0007|\u001B\\\\)", setOf(RegexOption.DOT_MATCHES_ALL))
    private val drop = Regex("\u001B(?:P|X|\\^|_).*?\u001B\\\\", setOf(RegexOption.DOT_MATCHES_ALL))
    private val controls = Regex("[\\u0000-\\u0008\\u000B\\u000C\\u000E-\\u001F\\u007F]")

    fun toDisplay(text: String): String {
        val withoutOsc = osc.replace(text, "")
        val withoutDrop = drop.replace(withoutOsc, "")
        val withoutCsi = csi.replace(withoutDrop, "")
        return controls.replace(withoutCsi, "")
    }
}
