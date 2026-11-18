import SwiftUI
import UIKit

struct NetworkDetails: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        let link = model.piState?.link
        VStack(alignment: .leading, spacing: 6) {
            Divider().overlay(Theme.line)
            if let ms = model.phoneRoundTripMS {
                LabelledRow(name: "Phone to server", value: String(format: "%.0f ms there and back", ms))
            }
            if let link {
                LabelledRow(name: "Car computer uses", value: link.viaText)
                if let median = link.medianMS {
                    let slowest = link.slowestMS.map { String(format: ", slowest %.0f", $0) } ?? ""
                    LabelledRow(name: "Its uploads, last minute", value: String(format: "%.0f ms median%@", median, slowest))
                }
                if let pushes = link.pushes {
                    let failed = Int(link.failed ?? 0)
                    let moved = Int(link.failovers ?? 0)
                    LabelledRow(name: "Sent / failed", value: "\(Int(pushes)) / \(failed)" + (moved > 0 ? " · \(moved) over another network" : ""))
                }
                let others = (link.networks ?? []).dropFirst()
                LabelledRow(name: "Fallback", value: others.isEmpty ? "none" : others.joined(separator: ", "))
                if let dbm = link.wifiDBm {
                    LabelledRow(name: "Wi-Fi signal", value: String(format: "%.0f dBm, %@", dbm, SignalWords.wifi(dbm)))
                }
                if let stick = link.stick { stickRows(stick) }
                if let synced = link.clockSynced {
                    LabelledRow(name: "Clock", value: synced ? "set from the network" : "not set yet")
                }
            } else if model.piState != nil {
                Text("This Pi's software does not send connection details yet; they come with release 9.")
                    .font(.footnote).foregroundColor(Theme.dim)
            }
        }
        .accessibilityIdentifier("network-details")
    }

    @ViewBuilder private func stickRows(_ s: PiLink.Stick) -> some View {
        if s.connected == false {
            LabelledRow(name: "4G stick", value: s.downS.map { "no data session for \(Format.duration($0))" } ?? "no data session")
        } else {
            LabelledRow(name: "4G stick", value: [s.network, s.provider].compactMap { $0 }.joined(separator: " · ").ifEmpty("connected"))
        }
        if let rsrp = s.rsrp {
            let bars = s.bars.map { String(format: " · %.0f bars", $0) } ?? ""
            LabelledRow(name: "Signal", value: String(format: "%.0f dBm, %@%@", rsrp, SignalWords.rsrp(rsrp), bars))
        }
        if let rsrq = s.rsrq, let snr = s.snr {
            LabelledRow(name: "Quality", value: String(format: "RSRQ %.0f dB · SNR %.1f dB", rsrq, snr))
        }
        if let ping = s.pingMS {
            let worst = s.pingWorstMS.map { String(format: ", worst %.0f", $0) } ?? ""
            let lost = (s.pingLost ?? 0) > 0 ? String(format: ", %.0f lost", s.pingLost ?? 0) : ""
            LabelledRow(name: "Stick to server", value: String(format: "%.0f ms%@%@", ping, worst, lost))
        }
        if let roaming = s.roaming { LabelledRow(name: "Data roaming", value: roaming ? "on" : "off") }
        if let cell = s.cell { LabelledRow(name: "Cell", value: cell) }
    }
}
