package nl.slot17.app

import android.Manifest
import android.app.Application
import android.content.res.Configuration
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.platform.LocalConfiguration
import nl.slot17.app.ui.MainScreen
import nl.slot17.app.ui.Slot17Theme

class Slot17App : Application() {
    lateinit var model: AppModel
        private set

    override fun onCreate() {
        super.onCreate()
        model = AppModel(this)
    }
}

class MainActivity : ComponentActivity() {
    private val model get() = (application as Slot17App).model

    private val ask = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { granted ->
        model.noticePermissionChanged()
        if (granted[Manifest.permission.ACCESS_FINE_LOCATION] == true && model.settings.recordLocation) model.location.start()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            Slot17Theme {
                val landscape = LocalConfiguration.current.orientation == Configuration.ORIENTATION_LANDSCAPE
                // The dashboard is for a phone in a holder: it may keep the screen on.
                LaunchedEffect(landscape, model.settings.keepScreenOn, model.tab) {
                    if (landscape && model.settings.keepScreenOn && model.tab == Tab.LIVE) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                    else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                }
                MainScreen(model)
            }
        }
        val wanted = buildList {
            if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
            if (model.settings.recordLocation) add(Manifest.permission.ACCESS_FINE_LOCATION)
        }.filter { checkSelfPermission(it) != android.content.pm.PackageManager.PERMISSION_GRANTED }
        if (wanted.isNotEmpty() && savedInstanceState == null) ask.launch(wanted.toTypedArray())
    }

    override fun onResume() {
        super.onResume()
        model.becameVisible()
    }

    override fun onPause() {
        model.becameHidden()
        super.onPause()
    }
}
