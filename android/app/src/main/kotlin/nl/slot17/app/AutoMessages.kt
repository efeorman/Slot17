package nl.slot17.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Person
import android.app.RemoteInput
import android.app.Service
import android.app.UiModeManager
import android.content.Context
import android.content.Intent
import android.content.res.Configuration
import android.graphics.drawable.Icon
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.annotation.TargetApi
import nl.slot17.core.CarMessages

/**
 * Urgent beeps as a conversation on the Android Auto screen, read out by Android Auto (see
 * developer.android.com/training/cars/messaging). Posted only while Android Auto is connected, so the
 * phone itself never gets these. A spoken reply becomes a sighting ([AutoReplyService]).
 *
 * Android Auto shows messages from an app that isn't from Google Play only with "Unknown sources" on
 * in its developer settings; the Settings screen says so.
 */
class AutoMessages(private val context: Context) {
    private val manager = context.getSystemService(NotificationManager::class.java)
    private val lines = ArrayDeque<Pair<String, Long>>()   // text and time, newest last
    private var lastAt: Double? = null

    init {
        manager.createNotificationChannel(NotificationChannel(CHANNEL, "Android Auto", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Urgent beeps as a message on the car screen, only while Android Auto is connected."
            setSound(null, null)       // the app beeps itself; Android Auto reads the message out
            enableVibration(false)
            setShowBadge(false)
        })
    }

    /** Whether the phone shows on a car screen right now: what androidx.car.app's CarConnection reads. */
    fun connected(): Boolean {
        val state = runCatching {
            context.contentResolver.query(Uri.parse("content://$CONNECTION"), arrayOf("CarConnectionState"), null, null, null)
                ?.use { c -> if (c.moveToFirst()) c.getInt(0) else 0 }
        }.getOrNull()
        if (state == PROJECTION) return true
        return context.getSystemService(UiModeManager::class.java)?.currentModeType == Configuration.UI_MODE_TYPE_CAR
    }

    /** Posts one urgent beep. Returns why nothing was posted, or null when it was. */
    fun post(text: String, now: Double, allowed: Boolean): String? {
        if (Build.VERSION.SDK_INT < 28) return "Android Auto needs Android 9"
        if (!allowed) return "notifications not allowed"
        if (!connected()) return NOT_CONNECTED
        if (!CarMessages.due(now, lastAt)) return "the last one was less than ${CarMessages.MIN_GAP_S.toInt()} s ago"
        lastAt = now
        lines.addLast(text to System.currentTimeMillis())
        while (lines.size > CarMessages.KEPT) lines.removeFirst()
        show(null)
        return null
    }

    /** After a reply: the app's answer in the conversation, which Android Auto expects. */
    fun answered(answer: String) {
        if (Build.VERSION.SDK_INT >= 28 && lines.isNotEmpty()) show(answer)
    }

    /** Marked as read, in the car or on the phone. */
    fun clear() {
        lines.clear()
        manager.cancel(ID)
    }

    @TargetApi(28)
    private fun show(answer: String?) {
        val slot17 = Person.Builder().setName("Slot17").setKey("slot17").build()
        val you = Person.Builder().setName("You").setKey("you").build()
        val style = Notification.MessagingStyle(you).setConversationTitle("Slot17").setGroupConversation(false)
        for ((line, at) in lines) style.addMessage(Notification.MessagingStyle.Message(line, at, slot17))
        answer?.let { style.addMessage(Notification.MessagingStyle.Message(it, System.currentTimeMillis(), slot17)) }
        val icon = Icon.createWithResource(context, R.drawable.ic_stat_slot17)
        // What NotificationCompat writes for setShowsUserInterface(false): Android Auto handles these itself.
        val noUi = Bundle().apply { putBoolean("android.support.action.showsUserInterface", false) }
        val reply = Notification.Action.Builder(icon, "Mark what you saw", pending(REPLY, mutable = true))
            .addRemoteInput(RemoteInput.Builder(KEY).setLabel("Police, ambulance, fire or nothing").build())
            .setSemanticAction(Notification.Action.SEMANTIC_ACTION_REPLY)
            .addExtras(noUi)
            .build()
        val read = Notification.Action.Builder(icon, "Mark as read", pending(READ, mutable = false))
            .setSemanticAction(Notification.Action.SEMANTIC_ACTION_MARK_AS_READ)
            .addExtras(Bundle(noUi))
            .build()
        manager.notify(ID, Notification.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_slot17)
            .setStyle(style)
            .setCategory(Notification.CATEGORY_MESSAGE)
            .setContentIntent(Notices.openApp(context))
            .addAction(reply)
            .addAction(read)
            .build())
    }

    private fun pending(action: String, mutable: Boolean): PendingIntent =
        PendingIntent.getService(context, if (mutable) 31 else 32, Intent(context, AutoReplyService::class.java).setAction(action),
            (if (mutable) PendingIntent.FLAG_MUTABLE else PendingIntent.FLAG_IMMUTABLE) or PendingIntent.FLAG_UPDATE_CURRENT)

    companion object {
        const val CHANNEL = "auto"
        const val ID = 18
        const val KEY = "seen"
        const val REPLY = "nl.slot17.app.AUTO_REPLY"
        const val READ = "nl.slot17.app.AUTO_READ"
        const val NOT_CONNECTED = "Android Auto not connected"
        private const val CONNECTION = "androidx.car.app.connection"
        private const val PROJECTION = 2   // CarConnection.CONNECTION_TYPE_PROJECTION
    }
}

/** Android Auto's reply and mark-as-read buttons. A service, as Android Auto asks: never an activity. */
class AutoReplyService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val model = (application as Slot17App).model
        if (intent != null) when (intent.action) {
            AutoMessages.REPLY -> model.carReply(
                RemoteInput.getResultsFromIntent(intent)?.getCharSequence(AutoMessages.KEY)?.toString().orEmpty())
            AutoMessages.READ -> model.carRead()
        }
        stopSelf(startId)
        return START_NOT_STICKY
    }
}
