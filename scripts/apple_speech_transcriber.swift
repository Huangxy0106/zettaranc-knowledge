import AVFoundation
import Darwin
import Foundation
import Speech

@available(macOS 26.0, *)
struct TranscriptRow: Codable {
    let start_ms: Int
    let end_ms: Int
    let text: String
    let final: Bool
}

@available(macOS 26.0, *)
struct CommandOutput: Codable {
    let available: Bool
    let requested_locale: String
    let selected_locale: String?
    let installed: Bool
    let processing_seconds: Double?
    let segments: [TranscriptRow]
    let error: String?
}

@available(macOS 26.0, *)
func emit(_ output: CommandOutput, to handle: FileHandle = .standardOutput) {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    let data = try! encoder.encode(output)
    handle.write(data)
    handle.write(Data("\n".utf8))
}

@available(macOS 26.0, *)
func ensureAssets(for transcriber: SpeechTranscriber, install: Bool) async throws -> Bool {
    let modules: [any SpeechModule] = [transcriber]
    var status = await AssetInventory.status(forModules: modules)
    if status == .installed {
        return true
    }
    guard install else {
        return false
    }
    if let request = try await AssetInventory.assetInstallationRequest(supporting: modules) {
        try await request.downloadAndInstall()
    }
    status = await AssetInventory.status(forModules: modules)
    return status == .installed
}

@available(macOS 26.0, *)
func transcribeFile(
    _ path: String,
    locale requestedLocale: Locale,
    installAssets: Bool
) async throws -> CommandOutput {
    guard SpeechTranscriber.isAvailable else {
        return CommandOutput(
            available: false,
            requested_locale: requestedLocale.identifier,
            selected_locale: nil,
            installed: false,
            processing_seconds: nil,
            segments: [],
            error: "SpeechTranscriber is unavailable on this device"
        )
    }
    guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: requestedLocale) else {
        return CommandOutput(
            available: true,
            requested_locale: requestedLocale.identifier,
            selected_locale: nil,
            installed: false,
            processing_seconds: nil,
            segments: [],
            error: "requested locale is unsupported"
        )
    }

    let transcriber = SpeechTranscriber(
        locale: locale,
        preset: .timeIndexedProgressiveTranscription
    )
    let installed = try await ensureAssets(for: transcriber, install: installAssets)
    guard installed else {
        return CommandOutput(
            available: true,
            requested_locale: requestedLocale.identifier,
            selected_locale: locale.identifier,
            installed: false,
            processing_seconds: nil,
            segments: [],
            error: "language assets are not installed"
        )
    }

    let audioURL = URL(fileURLWithPath: path)
    let audioFile = try AVAudioFile(forReading: audioURL)
    let analyzer = SpeechAnalyzer(modules: [transcriber])
    let resultTask = Task<[TranscriptRow], Error> {
        var rows: [TranscriptRow] = []
        for try await result in transcriber.results {
            guard result.isFinal else { continue }
            let text = String(result.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            let start = max(0.0, CMTimeGetSeconds(result.range.start))
            let duration = max(0.0, CMTimeGetSeconds(result.range.duration))
            rows.append(
                TranscriptRow(
                    start_ms: Int((start * 1000.0).rounded()),
                    end_ms: Int(((start + duration) * 1000.0).rounded()),
                    text: text,
                    final: true
                )
            )
        }
        return rows
    }

    let started = ContinuousClock.now
    _ = try await analyzer.analyzeSequence(from: audioFile)
    try await analyzer.finalizeAndFinishThroughEndOfInput()
    let rows = try await resultTask.value
    let elapsed = started.duration(to: .now)
    let processingSeconds = Double(elapsed.components.seconds)
        + Double(elapsed.components.attoseconds) / 1_000_000_000_000_000_000.0
    return CommandOutput(
        available: true,
        requested_locale: requestedLocale.identifier,
        selected_locale: locale.identifier,
        installed: true,
        processing_seconds: processingSeconds,
        segments: rows,
        error: nil
    )
}

@main
struct AppleSpeechTranscriberCLI {
    static func main() async {
        guard #available(macOS 26.0, *) else {
            FileHandle.standardError.write(Data("macOS 26 or newer is required\n".utf8))
            exit(2)
        }

        let arguments = Array(CommandLine.arguments.dropFirst())
        let localeIdentifier = arguments.first(where: { $0.hasPrefix("--locale=") })?
            .replacingOccurrences(of: "--locale=", with: "") ?? "zh-CN"
        let installAssets = arguments.contains("--install-assets")
        let positional = arguments.filter { !$0.hasPrefix("--") }
        let locale = Locale(identifier: localeIdentifier)

        if arguments.contains("--status") {
            let selected = await SpeechTranscriber.supportedLocale(equivalentTo: locale)
            let installedLocales = await SpeechTranscriber.installedLocales
            let installed = selected.map { selectedLocale in
                installedLocales.contains { $0.identifier == selectedLocale.identifier }
            } ?? false
            emit(
                CommandOutput(
                    available: SpeechTranscriber.isAvailable,
                    requested_locale: locale.identifier,
                    selected_locale: selected?.identifier,
                    installed: installed,
                    processing_seconds: nil,
                    segments: [],
                    error: selected == nil ? "requested locale is unsupported" : nil
                )
            )
            return
        }

        guard positional.count == 1 else {
            FileHandle.standardError.write(
                Data("usage: apple-speech-transcriber [--locale=zh-CN] [--install-assets] <audio-file>\n".utf8)
            )
            exit(2)
        }
        do {
            emit(
                try await transcribeFile(
                    positional[0],
                    locale: locale,
                    installAssets: installAssets
                )
            )
        } catch {
            emit(
                CommandOutput(
                    available: SpeechTranscriber.isAvailable,
                    requested_locale: locale.identifier,
                    selected_locale: nil,
                    installed: false,
                    processing_seconds: nil,
                    segments: [],
                    error: "\(type(of: error)): \(error.localizedDescription)"
                ),
                to: .standardError
            )
            exit(1)
        }
    }
}
