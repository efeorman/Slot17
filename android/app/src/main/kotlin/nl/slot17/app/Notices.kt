package nl.slot17.app

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import nl.slot17.core.FaultNotices
import nl.slot17.core.Sighting

/** Notification channels and the notices the app posts. */
class Notices(private val context: Context) {
    private val manager = context.getSystemService(NotificationManager::class.java)
    private var nextId = 100

    init {
        manager.createNotificationChannels(listOf(
            NotificationChannel(LIVE, "While listening", NotificationManager.IMPORTANCE_LOW).apply {
                description = "The ongoing notice while Slot17 listens: strongest channel, last beep, and buttons."
                setShowBadge(false)
            },
            NotificationChannel(FAULTS, "Car computer faults", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Detector degraded, receiver hears nothing, detector stopped, and when all is well again."
            },
            NotificationChannel(UPDATES, "Updates", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "A new app or car computer version is out."
            },
        ))
    }

    val allowed: Boolean
        get() = Build.VERSION.SDK_INT < 33 ||
            context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    val permission: String get() = if (allowed && manager.areNotificationsEnabled()) "Allowed" else "Not allowed"

    fun postFault(notice: FaultNotices.Notice) = post(FAULTS, notice.title, notice.body)

    fun postUpdate(text: String) = post(UPDATES, "Slot17: update available", "New: $text. See Settings, About.")

    private fun post(channel: String, title: String, body: String) {
        if (!allowed) return
        manager.notify(nextId++, Notification.Builder(context, channel)
            .setSmallIcon(R.drawable.ic_stat_slot17)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(Notification.BigTextStyle().bigText(body))
            .setContentIntent(openApp(context))
            .setAutoCancel(true)
            .build())
    }

    companion object {
        const val LIVE = "live"
        const val FAULTS = "faults"
        const val UPDATES = "updates"
        const val LIVE_ID = 17

        fun openApp(context: Context): PendingIntent = PendingIntent.getActivity(context, 0,
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

        fun action(context: Context, code: Int, action: String, label: Sighting.Label? = null): PendingIntent =
            PendingIntent.getBroadcast(context, code, Intent(context, ActionReceiver::class.java).setAction(action).apply {
                label?.let { putExtra(ActionReceiver.LABEL, it.name) }
            }, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }
}

/** What the ongoing notice shows; built by the model, drawn here. */
data class LiveNotice(
    val title: String,
    val text: String,
    val muted: Boolean,
    /** A new beep, a problem or STRONG: shown at once instead of waiting for the next regular update. */
    val important: Boolean,
)

object LiveNotification {
    fun build(context: Context, n: LiveNotice): Notification {
        val b = Notification.Builder(context, Notices.LIVE)
            .setSmallIcon(R.drawable.ic_stat_slot17)
            .setContentTitle(n.title)
            .setContentText(n.text)
            .setStyle(Notification.BigTextStyle().bigText(n.text))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setCategory(Notification.CATEGORY_NAVIGATION)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .setContentIntent(Notices.openApp(context))
            .addAction(Notification.Action.Builder(null, "Saw police",
                Notices.action(context, 1, ActionReceiver.MARK, Sighting.Label.POLICE)).build())
            .addAction(Notification.Action.Builder(null, "Saw nothing",
                Notices.action(context, 2, ActionReceiver.MARK, Sighting.Label.NOTHING)).build())
            .addAction(Notification.Action.Builder(null, if (n.muted) "Unmute" else "Mute 30 s",
                Notices.action(context, 3, ActionReceiver.MUTE)).build())
        if (Build.VERSION.SDK_INT >= 31) b.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE)
        // Android 16's Live Updates: asked for by name, so older SDKs simply skip it.
        runCatching { b.javaClass.getMethod("setRequestPromotedOngoing", Boolean::class.javaPrimitiveType).invoke(b, true) }
        return b.build()
    }
}
