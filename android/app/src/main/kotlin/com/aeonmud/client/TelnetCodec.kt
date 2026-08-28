package com.aeonmud.client

import java.io.ByteArrayOutputStream

data class TelnetFrame(
    val output: ByteArray,
    val response: ByteArray,
) {
    override fun equals(other: Any?): Boolean {
        return other is TelnetFrame &&
            output.contentEquals(other.output) &&
            response.contentEquals(other.response)
    }

    override fun hashCode(): Int = 31 * output.contentHashCode() + response.contentHashCode()
}

class TelnetCodec(
    terminalType: String = "ANSI",
) {
    private enum class State {
        Data,
        Iac,
        Negotiation,
        Subnegotiation,
        SubnegotiationIac,
    }

    private var state = State.Data
    private var pendingCommand = 0
    private val subnegotiation = ByteArrayOutputStream()
    private val terminalTypeBytes: ByteArray = run {
        val raw = terminalType.encodeToByteArray()
        if (raw.isEmpty()) "ANSI".encodeToByteArray() else raw.copyOf(raw.size.coerceAtMost(40))
    }
    private var width = 80
    private var height = 24

    fun setWindowSize(width: Int, height: Int): ByteArray {
        this.width = width.coerceIn(1, 65535)
        this.height = height.coerceIn(1, 65535)
        return nawsBytes()
    }

    fun feed(input: ByteArray): TelnetFrame {
        val output = ByteArrayOutputStream()
        val response = ByteArrayOutputStream()
        for (byte in input) {
            val value = byte.toInt() and 0xff
            when (state) {
                State.Data -> {
                    if (value == IAC) state = State.Iac else output.write(value)
                }
                State.Iac -> {
                    when (value) {
                        IAC -> {
                            output.write(IAC)
                            state = State.Data
                        }
                        DO, DONT, WILL, WONT -> {
                            pendingCommand = value
                            state = State.Negotiation
                        }
                        SB -> {
                            subnegotiation.reset()
                            state = State.Subnegotiation
                        }
                        SE, NOP -> state = State.Data
                        else -> state = State.Data
                    }
                }
                State.Negotiation -> {
                    response.writeAll(answerNegotiation(pendingCommand, value))
                    state = State.Data
                }
                State.Subnegotiation -> {
                    if (value == IAC) {
                        state = State.SubnegotiationIac
                    } else {
                        subnegotiation.write(value)
                    }
                }
                State.SubnegotiationIac -> {
                    when (value) {
                        IAC -> {
                            subnegotiation.write(IAC)
                            state = State.Subnegotiation
                        }
                        SE -> {
                            response.writeAll(answerSubnegotiation(subnegotiation.toByteArray()))
                            subnegotiation.reset()
                            state = State.Data
                        }
                        else -> state = State.Data
                    }
                }
            }
        }
        return TelnetFrame(output.toByteArray(), response.toByteArray())
    }

    private fun answerNegotiation(command: Int, option: Int): ByteArray {
        return when (command) {
            DO -> when (option) {
                NAWS -> command(WILL, option) + nawsBytes()
                TTYPE, SGA -> command(WILL, option)
                else -> command(WONT, option)
            }
            DONT -> command(WONT, option)
            WILL -> when (option) {
                ECHO, SGA -> command(DO, option)
                else -> command(DONT, option)
            }
            WONT -> command(DONT, option)
            else -> byteArrayOf()
        }
    }

    private fun answerSubnegotiation(data: ByteArray): ByteArray {
        if (data.size >= 2 && (data[0].toInt() and 0xff) == TTYPE && (data[1].toInt() and 0xff) == TTYPE_SEND) {
            return byteArrayOf(IAC.toByte(), SB.toByte(), TTYPE.toByte(), TTYPE_IS.toByte()) +
                terminalTypeBytes +
                byteArrayOf(IAC.toByte(), SE.toByte())
        }
        return byteArrayOf()
    }

    private fun command(command: Int, option: Int): ByteArray {
        return byteArrayOf(IAC.toByte(), command.toByte(), option.toByte())
    }

    private fun nawsBytes(): ByteArray {
        return byteArrayOf(
            IAC.toByte(),
            SB.toByte(),
            NAWS.toByte(),
            ((width ushr 8) and 0xff).toByte(),
            (width and 0xff).toByte(),
            ((height ushr 8) and 0xff).toByte(),
            (height and 0xff).toByte(),
            IAC.toByte(),
            SE.toByte(),
        )
    }

    companion object {
        const val IAC = 255
        const val DONT = 254
        const val DO = 253
        const val WONT = 252
        const val WILL = 251
        const val SB = 250
        const val SE = 240
        const val NOP = 241

        const val ECHO = 1
        const val SGA = 3
        const val TTYPE = 24
        const val NAWS = 31

        const val TTYPE_IS = 0
        const val TTYPE_SEND = 1
    }
}

private fun ByteArrayOutputStream.writeAll(bytes: ByteArray) {
    write(bytes, 0, bytes.size)
}
