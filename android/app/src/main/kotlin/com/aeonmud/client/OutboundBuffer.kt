package com.aeonmud.client

const val OutboundBufferChars = 1024
const val OutboundSpoofPrefix = "spoof "

fun splitOutboundBuffers(line: String): List<String> {
    if (line.length <= OutboundBufferChars) return listOf(line)
    val buffers = mutableListOf(line.take(OutboundBufferChars))
    var offset = OutboundBufferChars
    val continuationSize = OutboundBufferChars - OutboundSpoofPrefix.length
    while (offset < line.length) {
        val next = line.substring(offset, (offset + continuationSize).coerceAtMost(line.length))
        buffers += OutboundSpoofPrefix + next
        offset += continuationSize
    }
    return buffers
}
