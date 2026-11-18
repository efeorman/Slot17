import CoreLocation
import Foundation

/// What a widget button does, in the app's process. The tap's own time is the sighting's time, whatever
/// happens after it. With the app running, as on a drive, it is an ordinary sighting.
enum WidgetMarks {
    static func record(kind: String, tappedAt: Date) {
        guard let label = Sighting.Label(rawValue: kind) else { return }
        if let model = AppModel.current {
            model.markFromWidget(label, tappedAt: tappedAt)
            return
        }
        var place: Place?
        if let l = CLLocationManager().location, abs(l.timestamp.timeIntervalSinceNow) < 30,
           l.horizontalAccuracy >= 0, l.horizontalAccuracy < 200 {
            place = Place(lat: l.coordinate.latitude, lon: l.coordinate.longitude,
                          speedKmh: l.speed >= 0 ? l.speed * 3.6 : nil, accuracyM: l.horizontalAccuracy)
        }
        MarkInbox.standard.add(PendingMark(label: label, seenAt: tappedAt, place: place, id: Sighting.newID()))
        AppModel.shareMark(label, at: tappedAt)
    }
}
