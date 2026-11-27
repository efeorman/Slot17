import SwiftUI
import UIKit

/// iPhone's rotation lock would otherwise block the dashboard, so the app can ask for it.
final class OrientationControl: NSObject, UIApplicationDelegate {
    static var allowed: UIInterfaceOrientationMask = .allButUpsideDown

    func application(_ application: UIApplication,
                     supportedInterfaceOrientationsFor window: UIWindow?) -> UIInterfaceOrientationMask {
        Self.allowed
    }

    /// Without telling iOS again, it keeps the old limit and the phone never turns sideways.
    static func release() {
        allowed = .allButUpsideDown
        guard let scene = UIApplication.shared.connectedScenes.first as? UIWindowScene else { return }
        scene.keyWindow?.rootViewController?.setNeedsUpdateOfSupportedInterfaceOrientations()
    }

    func applicationWillTerminate(_ application: UIApplication) {
        AppModel.current?.appIsClosing()
    }

    static func request(_ mask: UIInterfaceOrientationMask) {
        allowed = mask
        guard let scene = UIApplication.shared.connectedScenes.first as? UIWindowScene else { return }
        scene.keyWindow?.rootViewController?.setNeedsUpdateOfSupportedInterfaceOrientations()
        scene.requestGeometryUpdate(.iOS(interfaceOrientations: mask)) { _ in }
    }
}

@main
struct Slot17App: App {
    @UIApplicationDelegateAdaptor(OrientationControl.self) private var orientation
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .preferredColorScheme(.dark)
                .onChange(of: phase) { newPhase in
                    switch newPhase {
                    case .active: model.becameActive()
                    case .background: model.movedToBackground()
                    default: break
                    }
                }
        }
    }
}

struct RootView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.verticalSizeClass) private var verticalSize
    @State private var forcedDashboard = false

    private var landscape: Bool { verticalSize == .compact }

    var body: some View {
        ZStack {
            Theme.background.ignoresSafeArea()
            if landscape && model.tab == .live {
                DashboardView(close: forcedDashboard ? closeDashboard : nil)
            } else {
                VStack(spacing: 0) {
                    NavigationStack {
                        ZStack {
                            Theme.background.ignoresSafeArea()
                            // Clipped so scrolled content does not run under the clock.
                            screen.clipped()
                        }
                        .toolbar(.hidden, for: .navigationBar)
                    }
                    TabBar()
                }
            }
        }
        .onChange(of: landscape) { _ in applyScreenAwake() }
        .onChange(of: model.tab) { _ in applyScreenAwake() }
        .onChange(of: model.settings.keepScreenOn) { _ in applyScreenAwake() }
        .onAppear { applyScreenAwake() }
    }

    @ViewBuilder private var screen: some View {
        switch model.tab {
        case .live:
            VStack(spacing: 0) {
                if !model.hasLink && !model.demo && !model.bluetooth.paired {
                    SetupCard().padding(.horizontal, 16).padding(.top, 8)
                }
                LiveView(openDashboard: openDashboard)
            }
        case .sightings: SightingsView()
        case .pi: PiView()
        case .logs: LogsView()
        case .settings: SettingsView()
        }
    }

    private func openDashboard() {
        forcedDashboard = true
        OrientationControl.request(.landscapeRight)
    }

    private func closeDashboard() {
        forcedDashboard = false
        OrientationControl.request(.portrait)
        // Hand rotation back to the phone once it has turned.
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { OrientationControl.release() }
    }

    private func applyScreenAwake() {
        UIApplication.shared.isIdleTimerDisabled = landscape && model.tab == .live && model.settings.keepScreenOn
    }
}

struct TabBar: View {
    @EnvironmentObject private var model: AppModel

    private func symbol(_ tab: AppModel.Tab) -> String {
        switch tab {
        case .live: return "dot.radiowaves.left.and.right"
        case .sightings: return "eye.fill"
        case .pi: return "cpu"
        case .logs: return "list.bullet.rectangle"
        case .settings: return "gearshape.fill"
        }
    }

    var body: some View {
        HStack(spacing: 0) {
            ForEach(AppModel.Tab.allCases) { tab in
                Button { model.tab = tab } label: {
                    VStack(spacing: 3) {
                        Image(systemName: symbol(tab)).font(.system(size: 19))
                        Text(tab.rawValue).font(.caption2).lineLimit(1).minimumScaleFactor(0.7)
                    }
                    .foregroundColor(model.tab == tab ? Theme.green : Theme.dim)
                    .frame(maxWidth: .infinity, minHeight: 48)
                    .contentShape(Rectangle())
                }
                .accessibilityIdentifier("tab-\(tab.rawValue)")
                .accessibilityAddTraits(model.tab == tab ? .isSelected : [])
            }
        }
        .padding(.top, 6)
        .background(Theme.panel.ignoresSafeArea(edges: .bottom))
        .overlay(alignment: .top) { Rectangle().fill(Theme.line).frame(height: 1) }
    }
}
