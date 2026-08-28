package com.aeonmud.client

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.graphics.Color
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

class MainActivity : Activity() {
    private lateinit var powerManager: PowerManager
    private lateinit var statusText: TextView
    private lateinit var outputText: TextView
    private lateinit var scrollView: ScrollView
    private lateinit var hostEdit: EditText
    private lateinit var portEdit: EditText
    private lateinit var inputEdit: EditText
    private lateinit var connectButton: Button
    private lateinit var disconnectButton: Button
    private lateinit var sendButton: Button

    private val outputBuffer = StringBuilder()
    private var connection: MudConnection? = null
    private var connectionToken = 0
    private var receiverRegistered = false

    private val powerSaveReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            updatePowerGate()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        powerManager = getSystemService(PowerManager::class.java)
        buildUi()
        updatePowerGate()
    }

    override fun onStart() {
        super.onStart()
        val filter = IntentFilter(PowerManager.ACTION_POWER_SAVE_MODE_CHANGED)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(powerSaveReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("DEPRECATION")
            registerReceiver(powerSaveReceiver, filter)
        }
        receiverRegistered = true
        updatePowerGate()
    }

    override fun onStop() {
        disconnect()
        if (receiverRegistered) {
            unregisterReceiver(powerSaveReceiver)
            receiverRegistered = false
        }
        super.onStop()
    }

    private fun buildUi() {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(10), dp(12), dp(10))
            setBackgroundColor(Color.BLACK)
        }

        statusText = TextView(this).apply {
            applyTerminalText(14f)
            text = "Battery saver required"
        }
        root.addView(statusText, matchWrap())

        val targetRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        hostEdit = EditText(this).apply {
            applyField()
            hint = "Host"
            setText("127.0.0.1")
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
        }
        portEdit = EditText(this).apply {
            applyField()
            hint = "Port"
            setText("4000")
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        targetRow.addView(hostEdit, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        targetRow.addView(portEdit, LinearLayout.LayoutParams(dp(96), LinearLayout.LayoutParams.WRAP_CONTENT).withStartMargin(dp(8)))
        root.addView(targetRow, matchWrap())

        val buttonRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        connectButton = Button(this).apply {
            applyButton()
            text = "Connect"
            setOnClickListener { connect() }
        }
        disconnectButton = Button(this).apply {
            applyButton()
            text = "Disconnect"
            setOnClickListener { disconnect() }
        }
        buttonRow.addView(connectButton, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        buttonRow.addView(disconnectButton, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).withStartMargin(dp(8)))
        root.addView(buttonRow, matchWrap())

        scrollView = ScrollView(this).apply {
            setBackgroundColor(Color.rgb(4, 4, 4))
        }
        outputText = TextView(this).apply {
            applyTerminalText(13f)
            text = ""
            setPadding(dp(8), dp(8), dp(8), dp(8))
        }
        scrollView.addView(outputText, matchWrap())
        root.addView(scrollView, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f).withTopMargin(dp(8)))

        val inputRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        inputEdit = EditText(this).apply {
            applyField()
            hint = "Command"
            setSingleLine(true)
            imeOptions = EditorInfo.IME_ACTION_SEND
            setOnEditorActionListener { _, actionId, _ ->
                if (actionId == EditorInfo.IME_ACTION_SEND) {
                    sendCurrentLine()
                    true
                } else {
                    false
                }
            }
        }
        sendButton = Button(this).apply {
            applyButton()
            text = "Send"
            setOnClickListener { sendCurrentLine() }
        }
        inputRow.addView(inputEdit, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        inputRow.addView(sendButton, LinearLayout.LayoutParams(dp(88), LinearLayout.LayoutParams.WRAP_CONTENT).withStartMargin(dp(8)))
        root.addView(inputRow, matchWrap().withTopMargin(dp(8)))

        setContentView(root)
    }

    private fun connect() {
        if (!powerManager.isPowerSaveMode) {
            appendStatus("Enable system battery saver first.")
            updatePowerGate()
            return
        }
        val host = hostEdit.text.toString().trim()
        val port = portEdit.text.toString().trim().toIntOrNull()
        if (host.isEmpty() || port == null || port !in 1..65535) {
            appendStatus("Invalid host or port.")
            return
        }
        disconnect()
        val token = ++connectionToken
        val next = MudConnection(host, port) { event -> handleMudEvent(token, event) }
        connection = next
        appendStatus("Connecting to $host:$port")
        updatePowerGate()
        next.start()
    }

    private fun disconnect() {
        connectionToken++
        connection?.disconnect()
        connection = null
        updatePowerGate()
    }

    private fun sendCurrentLine() {
        val line = inputEdit.text.toString()
        if (line.isBlank()) return
        val active = connection
        if (active == null) {
            appendStatus("Not connected.")
            return
        }
        appendOutput("> $line\n")
        inputEdit.setText("")
        active.sendLine(line)
    }

    private fun handleMudEvent(token: Int, event: MudEvent) {
        runOnUiThread {
            if (token != connectionToken) return@runOnUiThread
            when (event) {
                is MudEvent.Output -> appendOutput(event.text)
                is MudEvent.Status -> appendStatus(event.text)
                is MudEvent.Closed -> {
                    appendStatus(event.text)
                    connection = null
                    updatePowerGate()
                }
            }
        }
    }

    private fun updatePowerGate() {
        if (!powerManager.isPowerSaveMode && connection != null) {
            appendStatus("Battery saver disabled; disconnecting.")
            disconnect()
            return
        }
        val powerSaver = powerManager.isPowerSaveMode
        val connected = connection != null
        connectButton.isEnabled = powerSaver && !connected
        disconnectButton.isEnabled = connected
        sendButton.isEnabled = powerSaver && connected
        if (!connected) {
            statusText.text = if (powerSaver) "Battery saver active" else "Battery saver required"
        }
    }

    private fun appendStatus(text: String) {
        statusText.text = text
        appendOutput("[client] $text\n")
    }

    private fun appendOutput(text: String) {
        outputBuffer.append(text)
        if (outputBuffer.length > MaxOutputChars) {
            outputBuffer.delete(0, outputBuffer.length - MaxOutputChars)
        }
        outputText.text = outputBuffer.toString()
        scrollView.post { scrollView.fullScroll(View.FOCUS_DOWN) }
    }

    private fun TextView.applyTerminalText(sizeSp: Float) {
        setTextColor(Color.rgb(214, 230, 214))
        typeface = Typeface.MONOSPACE
        textSize = sizeSp
        includeFontPadding = false
    }

    private fun EditText.applyField() {
        setTextColor(Color.rgb(230, 230, 230))
        setHintTextColor(Color.rgb(120, 130, 120))
        setBackgroundColor(Color.rgb(18, 18, 18))
        typeface = Typeface.MONOSPACE
        textSize = 14f
        setSingleLine(true)
        setPadding(dp(8), 0, dp(8), 0)
    }

    private fun Button.applyButton() {
        setTextColor(Color.WHITE)
        setBackgroundColor(Color.rgb(30, 48, 36))
        isAllCaps = false
        minHeight = dp(44)
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private fun matchWrap(): LinearLayout.LayoutParams {
        return LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
    }

    private fun LinearLayout.LayoutParams.withTopMargin(value: Int): LinearLayout.LayoutParams {
        topMargin = value
        return this
    }

    private fun LinearLayout.LayoutParams.withStartMargin(value: Int): LinearLayout.LayoutParams {
        marginStart = value
        return this
    }

    companion object {
        private const val MaxOutputChars = 40_000
    }
}
