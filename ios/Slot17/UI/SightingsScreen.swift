import SwiftUI
import UIKit

// MARK: Sightings

struct SightingsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var adding = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                ScreenTitle(text: "Sightings")
                Text("What you actually saw. These are what can show later whether alerts really go with vehicles.")
                    .font(.footnote).foregroundColor(Theme.dim)
                MarkSummaryCard()
                Button { adding = true } label: {
                    Label("Add a sighting with a note", systemImage: "plus.circle.fill")
                }
                .buttonStyle(ActionButtonStyle(tint: Theme.blue, foreground: .black))
                .accessibilityIdentifier("add-sighting")
                if model.sightings.isEmpty {
                    Card(title: nil) {
                        Text("Nothing yet. Tap Police, Ambulance or Fire on the Live screen when you see one, also when nothing beeped, and Saw nothing after a beep with nothing in sight.")
                            .font(.subheadline).foregroundColor(Theme.dim)
                    }
                }
                ForEach(model.sightings) { sighting in
                    SightingRow(sighting: sighting)
                }
            }
            .padding(16)
        }
        .sheet(isPresented: $adding) { AddSightingSheet() }
    }
}

private struct SightingRow: View {
    let sighting: Sighting
    @EnvironmentObject private var model: AppModel

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon).foregroundColor(colour).font(.title3).frame(width: 26)
            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    Text(sighting.isRecordingRequest ? "Saved recording" : sighting.label.title)
                        .font(.body.weight(.semibold)).foregroundColor(Theme.text)
                    Spacer()
                    Text(sighting.seenAt, format: .dateTime.day().month().hour().minute().second())
                        .font(.caption.monospacedDigit()).foregroundColor(Theme.dim)
                }
                if !sighting.note.isEmpty {
                    Text(sighting.note).font(.subheadline).foregroundColor(Theme.text)
                }
                Text(sighting.statusText).font(.footnote).foregroundColor(sighting.phase == .failed ? Theme.red : Theme.dim)
                    .accessibilityIdentifier("sighting-status")
            }
        }
        .padding(12)
        .background(Theme.panel)
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.line, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .contextMenu {
            Button(role: .destructive) { model.deleteSighting(sighting.id) } label: {
                Label("Remove from this phone", systemImage: "trash")
            }
        }
    }

    private var icon: String {
        switch sighting.phase {
        case .saved: return "checkmark.circle.fill"
        case .failed: return "exclamationmark.triangle.fill"
        default: return "clock.fill"
        }
    }

    private var colour: Color {
        switch sighting.phase {
        case .saved: return Theme.green
        case .failed: return Theme.red
        default: return Theme.dim
        }
    }
}

private struct AddSightingSheet: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var label = Sighting.Label.police
    @State private var minutesAgo = 0
    @State private var note = ""

    var body: some View {
        NavigationStack {
            Form {
                Picker("What", selection: $label) {
                    ForEach(Sighting.Label.allCases) { Text($0.title).tag($0) }
                }
                Picker("When", selection: $minutesAgo) {
                    Text("Now").tag(0)
                    Text("1 min ago").tag(1)
                    Text("2 min ago").tag(2)
                    Text("5 min ago").tag(5)
                    Text("10 min ago").tag(10)
                    Text("30 min ago").tag(30)
                }
                Section("Note") {
                    TextField("For example: blue lights, driving towards me", text: $note, axis: .vertical)
                        .lineLimit(3...6)
                        .accessibilityIdentifier("sighting-note")
                }
                Section {
                    Text("A recording is saved only for a sighting marked within about 5 seconds. Later ones are kept as a note.")
                        .font(.footnote)
                }
            }
            .navigationTitle("Add a sighting")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        model.mark(label, note: note, secondsAgo: TimeInterval(minutesAgo * 60))
                        dismiss()
                    }
                    .accessibilityIdentifier("save-sighting")
                }
            }
        }
    }
}

/// The marks against the sounds: how often each kind of sound came with a vehicle you saw.
private struct MarkSummaryCard: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        let s = model.markSummary
        Card(title: "What your marks show") {
            if s.urgent.played + s.chime.played + s.vehiclesMarked == 0 {
                Text("Nothing to show yet. Each sound counts towards your first mark in the \(Int(model.settings.summaryWindow / 60)) minutes after it.")
                    .font(.footnote).foregroundColor(Theme.dim)
            } else {
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                    GridRow {
                        Text("")
                        header("Played"); header("Vehicle"); header("Nothing"); header("No mark")
                    }
                    Divider().overlay(Theme.line).gridCellUnsizedAxes(.horizontal)
                    row("Urgent", s.urgent, Theme.red)
                    row("Chime", s.chime, Theme.yellow)
                }
                .accessibilityIdentifier("mark-summary")
                LabelledRow(name: "Vehicles you marked", value: "\(s.vehiclesMarked)")
                LabelledRow(name: "With no sound before them", value: "\(s.vehiclesWithoutSound)")
                Text("A vehicle with no sound before it is a possible miss. Nothing means you saw no vehicle, not that none was there.")
                    .font(.caption2).foregroundColor(Theme.dim)
            }
        }
    }

    private func header(_ text: String) -> some View {
        Text(text).font(.caption.weight(.semibold)).foregroundColor(Theme.dim).gridColumnAlignment(.trailing)
    }

    private func row(_ name: String, _ r: MarkSummary.Row, _ colour: Color) -> some View {
        GridRow {
            HStack(spacing: 6) {
                Circle().fill(colour).frame(width: 8, height: 8)
                Text(name).foregroundColor(Theme.text)
            }
            Text("\(r.played)").monospacedDigit().foregroundColor(Theme.text)
            Text("\(r.vehicleSeen)").monospacedDigit().foregroundColor(Theme.text)
            Text("\(r.nothingSeen)").monospacedDigit().foregroundColor(Theme.text)
            Text("\(r.unmarked)").monospacedDigit().foregroundColor(Theme.text)
        }
        .font(.subheadline)
    }
}
