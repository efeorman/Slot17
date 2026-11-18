import CoreLocation
import Foundation

/// The phone's position, recorded with alerts and sightings so later analysis knows the road type,
/// the speed and how far apart events were. It stays on the phone unless sent with drive data or a
/// sighting. It never affects whether a sound plays.
final class LocationService: NSObject, CLLocationManagerDelegate {
    var log: (String) -> Void = { _ in }
    var onChange: () -> Void = {}
    private let manager = CLLocationManager()
    private(set) var latest: CLLocation?
    private(set) var running = false
    private var lastTrackPoint = Date.distantPast
    var trackEvery: TimeInterval = 10
    private(set) var track: [TrackPoint] = []

    struct TrackPoint: Codable, Equatable {
        var at: Date
        var lat: Double
        var lon: Double
        var speedKmh: Double?
    }

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyNearestTenMeters
        // A place every 30 m is plenty for records taken every 10 s, and wakes the app a third as often.
        manager.distanceFilter = 30
        manager.pausesLocationUpdatesAutomatically = false
        manager.activityType = .automotiveNavigation
        track = JSONStore.load("track.json", as: [TrackPoint].self) ?? []
    }

    var statusText: String {
        switch manager.authorizationStatus {
        case .notDetermined: return "Not asked yet"
        case .denied: return "Not allowed (Settings > Privacy > Location)"
        case .restricted: return "Restricted on this phone"
        case .authorizedWhenInUse, .authorizedAlways:
            guard running else { return "Allowed, not recording" }
            guard let latest else { return "Waiting for a position" }
            return String(format: "Recording, accurate to %.0f m", latest.horizontalAccuracy)
        @unknown default: return "Unknown"
        }
    }

    private var lowPower = false

    /// Coarse and rare while nothing needs a precise place; the setting takes effect without restarting.
    func setLowPower(_ on: Bool) {
        guard on != lowPower else { return }
        lowPower = on
        manager.desiredAccuracy = on ? kCLLocationAccuracyThreeKilometers : kCLLocationAccuracyNearestTenMeters
        manager.distanceFilter = on ? 1000 : 30
        log(on ? "location on low power: no car data" : "location back to full accuracy")
    }

    /// Set by start(), cleared by stop().
    private var wanted = false

    /// Asks once, then keeps going in the background while the app runs its audio session.
    func start() {
        wanted = true
        guard !running else { return }
        switch manager.authorizationStatus {
        case .notDetermined:
            manager.requestWhenInUseAuthorization()
            return
        case .denied, .restricted:
            return
        default:
            break
        }
        manager.allowsBackgroundLocationUpdates = true
        manager.showsBackgroundLocationIndicator = true
        manager.startUpdatingLocation()
        running = true
        log("location recording started")
        onChange()
    }

    func stop() {
        wanted = false
        guard running else { return }
        manager.stopUpdatingLocation()
        running = false
        log("location recording stopped")
        saveTrack()
        onChange()
    }

    /// The current place, if a reasonably fresh and accurate fix exists.
    var place: Place? {
        guard let l = latest, abs(l.timestamp.timeIntervalSinceNow) < 30, l.horizontalAccuracy >= 0,
              l.horizontalAccuracy < 200 else { return nil }
        return Place(lat: l.coordinate.latitude, lon: l.coordinate.longitude,
                     speedKmh: l.speed >= 0 ? l.speed * 3.6 : nil, accuracyM: l.horizontalAccuracy)
    }

    func saveTrack() {
        if track.count > 20_000 { track.removeFirst(track.count - 20_000) }
        JSONStore.save(track, "track.json")
    }

    func clearTrack() {
        track.removeAll()
        saveTrack()
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        DispatchQueue.main.async {
            switch manager.authorizationStatus {
            // iOS calls this after every launch too, so it may only continue what was asked for.
            case .authorizedWhenInUse, .authorizedAlways:
                if self.wanted { self.start() } else { self.onChange() }
            default: self.onChange()
            }
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        // Negative accuracy means the fix is invalid.
        guard let location = locations.last, location.horizontalAccuracy >= 0,
              CLLocationCoordinate2DIsValid(location.coordinate) else { return }
        DispatchQueue.main.async {
            self.latest = location
            if location.timestamp.timeIntervalSince(self.lastTrackPoint) >= self.trackEvery, location.horizontalAccuracy < 200 {
                self.lastTrackPoint = location.timestamp
                self.track.append(TrackPoint(at: location.timestamp, lat: location.coordinate.latitude,
                                             lon: location.coordinate.longitude,
                                             speedKmh: location.speed >= 0 ? location.speed * 3.6 : nil))
                if self.track.count % 30 == 0 { self.saveTrack() }
            }
        }
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        DispatchQueue.main.async { self.log("location problem: \(error.localizedDescription)") }
    }
}
