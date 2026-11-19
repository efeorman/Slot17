package nl.slot17.app

import android.Manifest
import android.app.Service
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService
import android.widget.RemoteViews
import nl.slot17.core.Sighting

/**
 * Keeps Slot17 listening with the screen off: a foreground service with the ongoing notice, and a
 * partial wake lock so the link and the sound timers keep running. On iPhone the audio session does
 * this. It stops when the app goes to sleep after parking (Settings, Behaviour).
 */
class LinkService : Service() {
    private var wake: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val model = (application as Slot17App).model
        var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) {
            type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
        }
        val notification = LiveNotification.build(this, model.liveNotice())
        if (Build.VERSION.SDK_INT >= 29) startForeground(Notices.LIVE_ID, notification, type)
        else startForeground(Notices.LIVE_ID, notification)
        wake = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "slot17:listening").apply { acquire() }
        model.serviceRunning = true
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        wake?.let { if (it.isHeld) it.release() }
        (application as Slot17App).model.serviceRunning = false
        super.onDestroy()
    }

    companion object {
        fun start(context: Context) {
            runCatching { context.startForegroundService(Intent(context, LinkService::class.java)) }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, LinkService::class.java))
        }
    }
}

/** The buttons in the ongoing notice and on the home screen widget. */
class ActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val model = (context.applicationContext as Slot17App).model
        when (intent.action) {
            MARK -> intent.getStringExtra(LABEL)?.let { name ->
                runCatching { Sighting.Label.valueOf(name) }.getOrNull()?.let { model.markFromWidget(it, System.currentTimeMillis()) }
            }
            MUTE -> model.toggleMute()
        }
    }

    companion object {
        const val MARK = "nl.slot17.app.MARK"
        const val MUTE = "nl.slot17.app.MUTE"
        const val LABEL = "label"
    }
}

/** Home screen widget: the last sighting, and two buttons for the driver who is not looking. */
class MarkWidget : AppWidgetProvider() {
    override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) =
        refresh(context, (context.applicationContext as Slot17App).model.widgetLine())

    companion object {
        fun refresh(context: Context, line: String) {
            val manager = AppWidgetManager.getInstance(context) ?: return
            val ids = runCatching { manager.getAppWidgetIds(ComponentName(context, MarkWidget::class.java)) }.getOrNull() ?: return
            if (ids.isEmpty()) return
            val views = RemoteViews(context.packageName, R.layout.widget_mark).apply {
                setTextViewText(R.id.widget_status, line)
                setOnClickPendingIntent(R.id.widget_police, Notices.action(context, 11, ActionReceiver.MARK, Sighting.Label.POLICE))
                setOnClickPendingIntent(R.id.widget_nothing, Notices.action(context, 12, ActionReceiver.MARK, Sighting.Label.NOTHING))
                setOnClickPendingIntent(R.id.widget_title, Notices.openApp(context))
            }
            manager.updateAppWidget(ids, views)
        }
    }
}

/** Quick Settings tiles, the Android counterpart of the iPhone's Control Center controls. */
abstract class MarkTile(private val label: Sighting.Label) : TileService() {
    override fun onStartListening() {
        qsTile?.apply {
            state = Tile.STATE_INACTIVE
            if (Build.VERSION.SDK_INT >= 29) subtitle = "Slot17"
            updateTile()
        }
    }

    override fun onClick() {
        (application as Slot17App).model.markFromWidget(label, System.currentTimeMillis())
        qsTile?.apply {
            state = Tile.STATE_ACTIVE
            if (Build.VERSION.SDK_INT >= 29) subtitle = "Saved"
            updateTile()
        }
    }
}

class PoliceTile : MarkTile(Sighting.Label.POLICE)
class NothingTile : MarkTile(Sighting.Label.NOTHING)
