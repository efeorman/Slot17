package nl.slot17.app.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Description
import androidx.compose.material.icons.filled.Memory
import androidx.compose.material.icons.filled.Sensors
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import nl.slot17.app.AppModel
import nl.slot17.app.Tab

private fun icon(tab: Tab): ImageVector = when (tab) {
    Tab.LIVE -> Icons.Filled.Sensors
    Tab.SIGHTINGS -> Icons.Filled.Visibility
    Tab.PI -> Icons.Filled.Memory
    Tab.LOGS -> Icons.Filled.Description
    Tab.SETTINGS -> Icons.Filled.Settings
}

@Composable
fun MainScreen(model: AppModel) {
    var editingLink by remember { mutableStateOf(false) }
    Scaffold(containerColor = C.background, bottomBar = {
        NavigationBar(containerColor = C.panel) {
            for (tab in Tab.entries) NavigationBarItem(
                selected = model.tab == tab, onClick = { model.selectTab(tab) },
                icon = { Icon(icon(tab), null) }, label = { Text(tab.title) },
                colors = NavigationBarItemDefaults.colors(selectedIconColor = C.green, selectedTextColor = C.green,
                    unselectedIconColor = C.dim, unselectedTextColor = C.dim, indicatorColor = Color.Transparent),
            )
        }
    }) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            when (model.tab) {
                Tab.LIVE -> LiveScreen(model) { editingLink = true }
                Tab.SIGHTINGS -> SightingsScreen(model)
                Tab.PI -> PiScreen(model)
                Tab.LOGS -> LogsScreen(model)
                Tab.SETTINGS -> SettingsScreen(model) { editingLink = true }
            }
        }
    }
    if (editingLink) LinkDialog(model) { editingLink = false }
}
