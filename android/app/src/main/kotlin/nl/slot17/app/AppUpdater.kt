package nl.slot17.app

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.Uri
import android.os.Build
import android.provider.Settings
import java.net.HttpURLConnection
import java.net.URL
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Updates from inside the app: downloads Slot17.apk from the GitHub release and hands it to Android's own
 * installer, which asks the user to confirm. Android only accepts it as an update if it is signed with the
 * same key as the installed app, so a changed download can't install. Settings stay, as with any update.
 */
class AppUpdater(private val context: Context) {
    /** Android asks once whether Slot17 may install apps; until then this is false. */
    val allowed: Boolean get() = context.packageManager.canRequestPackageInstalls()

    fun askPermission() {
        context.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${context.packageName}"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    /** Download the release's APK into an install session and commit it; [progress] gets 0 to 100. */
    suspend fun install(tag: String, progress: (Int) -> Unit) = withContext(Dispatchers.IO) {
        require(Regex("v?\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}").matches(tag)) { "odd release tag" }
        val conn = (URL("https://github.com/$REPO/releases/download/$tag/Slot17.apk").openConnection() as HttpURLConnection).apply {
            connectTimeout = 15_000
            readTimeout = 30_000
            instanceFollowRedirects = true     // to GitHub's download servers, https only
        }
        try {
            if (conn.responseCode != 200) error("GitHub answered ${conn.responseCode}")
            val length = conn.contentLengthLong
            if (length <= 0 || length > MAX_BYTES) error("unexpected size")
            val installer = context.packageManager.packageInstaller
            val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
                setAppPackageName(context.packageName)
                setSize(length)
            }
            val id = installer.createSession(params)
            installer.openSession(id).use { session ->
                conn.inputStream.use { input ->
                    session.openWrite("Slot17.apk", 0, length).use { out ->
                        val buf = ByteArray(64 * 1024)
                        var done = 0L
                        while (true) {
                            val n = input.read(buf)
                            if (n < 0) break
                            out.write(buf, 0, n)
                            done += n
                            progress((done * 100 / length).toInt())
                        }
                        session.fsync(out)
                    }
                }
                val result = PendingIntent.getBroadcast(context, 41, Intent(context, InstallResultReceiver::class.java),
                    PendingIntent.FLAG_MUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
                session.commit(result.intentSender)
            }
        } finally {
            conn.disconnect()
        }
    }

    companion object {
        const val REPO = "efeorman/Slot17"
        private const val MAX_BYTES = 60L shl 20
    }
}

/** Android's answer to an install: show its confirmation screen, or tell the model what went wrong. */
class InstallResultReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val model = (context.applicationContext as Slot17App).model
        when (val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                val confirm = if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                else @Suppress("DEPRECATION") intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
                confirm?.let { context.startActivity(it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
                model.updateState = "Confirm the update in Android's window"
            }
            PackageInstaller.STATUS_SUCCESS -> model.updateState = "Updated"
            else -> model.updateState = "Not installed (${intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE) ?: status})"
        }
    }
}
