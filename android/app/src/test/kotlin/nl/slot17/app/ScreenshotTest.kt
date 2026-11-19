package nl.slot17.app

import android.graphics.Bitmap
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.captureToImage
import androidx.activity.ComponentActivity
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onRoot
import java.io.File
import java.time.Duration
import nl.slot17.app.ui.MainScreen
import nl.slot17.app.ui.Slot17Theme
import nl.slot17.core.Sighting
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import org.robolectric.shadows.ShadowSystemClock

/**
 * Draws every screen with demo data and saves it as a PNG (build/screenshots), so a build shows
 * what changed without an emulator or a phone. It also proves each screen draws without crashing.
 */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [35], qualifiers = "w393dp-h852dp-xxhdpi", application = Slot17App::class)
class ScreenshotTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>().apply { mainClock.autoAdvance = false }

    private val out = File(System.getProperty("slot17.screens") ?: "build/screenshots").apply { mkdirs() }

    private fun save(name: String) {
        // A tab change recomposes on the next frames: let a few pass before taking the picture.
        repeat(5) { compose.mainClock.advanceTimeByFrame(); compose.waitForIdle() }
        compose.mainClock.advanceTimeBy(1_000)
        compose.waitForIdle()
        val bitmap = runCatching { compose.onRoot().captureToImage().asAndroidBitmap() }.getOrElse {
            // Fallback: draw the whole window into a picture ourselves.
            val view = compose.activity.window.decorView
            Bitmap.createBitmap(view.width, view.height, Bitmap.Config.ARGB_8888).also { view.draw(android.graphics.Canvas(it)) }
        }
        File(out, "android-$name.png").outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }

    @Test
    fun everyScreen() {
        val model = (RuntimeEnvironment.getApplication() as Slot17App).model
        model.screenshotValues = true
        model.switchDemo(true)
        model.stopTickLoop()
        repeat(4) {
            ShadowSystemClock.advanceBy(Duration.ofMillis(300))
            model.tickNow()
        }
        model.mark(Sighting.Label.POLICE, "blue lights, driving towards me")
        compose.setContent { Slot17Theme { MainScreen(model) } }
        for (tab in Tab.entries) {
            model.selectTab(tab)
            ShadowSystemClock.advanceBy(Duration.ofMillis(300))
            model.tickNow()
            save(tab.name.lowercase())
        }
        val files = out.listFiles { f -> f.name.startsWith("android-") }?.size ?: 0
        assertTrue("a picture of every screen", files >= Tab.entries.size)
    }

    @Test
    fun firstStartWithoutALink() {
        val model = (RuntimeEnvironment.getApplication() as Slot17App).model
        model.stopTickLoop()
        compose.setContent { Slot17Theme { MainScreen(model) } }
        save("first-start")
    }
}
