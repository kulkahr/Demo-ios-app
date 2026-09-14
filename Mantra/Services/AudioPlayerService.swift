import AVFoundation
import Combine
import Foundation

/// AVFoundation playback for chant audio: pitch-preserving speed control,
/// loop counts, and sample-accurate position reporting for karaoke sync.
@MainActor
final class AudioPlayerService: ObservableObject {
    enum PlaybackState: Equatable {
        case idle
        case playing
        case paused
        case finished
    }

    // MARK: - Published state

    @Published private(set) var state: PlaybackState = .idle
    /// Position within the current loop, in seconds.
    @Published private(set) var currentTime: Double = 0
    @Published private(set) var duration: Double = 0
    /// 1-based index of the loop currently sounding.
    @Published private(set) var loopIndex: Int = 1
    @Published var speed: Float = 1.0
    @Published var totalLoops: Int = 1

    /// Fires each time a loop completes (for session recording).
    let loopDidComplete = PassthroughSubject<Int, Never>()

    // MARK: - Engine

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let timePitch = AVAudioUnitTimePitch()
    private var file: AVAudioFile?
    private var currentURL: URL?
    private var positionTimer: Timer?
    private var loopsRemaining = 0
    /// Bumped on every stop/seek; stale completion handlers are discarded.
    private var scheduleGeneration = 0
    /// Pause point; play() resumes from here after a seek while paused.
    private var pausedFrame: AVAudioFramePosition?

    init() {
        engine.attach(player)
        engine.attach(timePitch)
        engine.connect(player, to: timePitch, format: nil)
        engine.connect(timePitch, to: engine.mainMixerNode, format: nil)
        configureAudioSession()
    }

    /// Audible playback regardless of the silent switch; required on device.
    private func configureAudioSession() {
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {
            // Session activation failure only degrades background behavior.
        }
    }

    var isPlaying: Bool { state == .playing }

    // MARK: - Loading

    /// Loads a WAV file and prepares playback. Returns the audio duration.
    @discardableResult
    func load(url: URL) throws -> Double {
        stop()
        let audioFile = try AVAudioFile(forReading: url)
        file = audioFile
        currentURL = url
        duration = Double(audioFile.length) / audioFile.processingFormat.sampleRate
        loopIndex = 1
        currentTime = 0
        return duration
    }

    // MARK: - Transport

    func play() {
        guard let file, currentURL != nil else { return }
        if state == .paused {
            if let frame = pausedFrame {
                // Resume from a seek-while-paused (or idle-seek) position: the
                // original segment was invalidated by player.stop(), so
                // schedule a new one starting at the seek target.
                startEngineIfNeeded()
                scheduleCurrentFile(startingAt: frame)
                player.play()
                pausedFrame = nil
            } else {
                // Plain pause/resume: the segment is still scheduled.
                player.play()
            }
            state = .playing
            startPositionTimer()
            return
        }
        guard state == .idle || state == .finished else { return }

        // Configure loop scheduling.
        loopsRemaining = max(totalLoops - 1, 0)
        loopIndex = 1

        startEngineIfNeeded()
        scheduleCurrentFile()
        player.play()
        timePitch.rate = speed
        state = .playing
        startPositionTimer()
    }

    func pause() {
        guard state == .playing else { return }
        player.pause()
        // The scheduled segment stays on the node, so a plain resume via
        // play() continues it; pausedFrame stays nil for that path.
        state = .paused
        stopPositionTimer()
    }

    /// Stops playback and rewinds to the beginning.
    func stop() {
        scheduleGeneration &+= 1
        stopPositionTimer()
        player.stop()
        if engine.isRunning {
            engine.stop()
        }
        player.reset()
        pausedFrame = nil
        currentTime = 0
        loopIndex = 1
        state = .idle
    }

    /// Jumps to a position (seconds) inside the current loop. Works while
    /// playing, paused, or idle: play() always resumes from the seek point.
    func seek(to seconds: Double) {
        guard let file, duration > 0 else { return }
        let clamped = max(0, min(seconds, duration))
        let frame = AVAudioFramePosition(clamped * file.processingFormat.sampleRate)

        scheduleGeneration &+= 1
        let generation = scheduleGeneration
        let wasPlaying = state == .playing
        let wasIdle = state == .idle || state == .finished

        player.stop()
        currentTime = clamped

        if wasPlaying {
            startEngineIfNeeded()
            scheduleCurrentFile(startingAt: frame, generation: generation)
            player.play()
            pausedFrame = nil
        } else {
            if wasIdle {
                // A seek from idle/finished arms a fresh chant session.
                loopsRemaining = max(totalLoops - 1, 0)
                loopIndex = 1
            }
            pausedFrame = frame
            state = .paused
        }
    }

    // MARK: - Scheduling

    private func scheduleCurrentFile(
        startingAt frame: AVAudioFramePosition? = nil,
        generation: Int? = nil
    ) {
        guard let file else { return }
        let gen = generation ?? scheduleGeneration
        let startFrame = frame ?? 0
        file.framePosition = startFrame
        let framesRemaining = file.length - startFrame
        guard framesRemaining > 0 else { return }

        player.scheduleSegment(
            file,
            startingFrame: startFrame,
            frameCount: AVAudioFrameCount(framesRemaining),
            at: nil
        ) { [weak self] in
            guard let self else { return }
            Task { @MainActor in
                // AVAudioPlayerNode fires pending completion handlers when
                // stop() is called (stop or seek); discard stale callbacks.
                guard self.scheduleGeneration == gen else { return }
                self.handleSegmentCompletion()
            }
        }
    }

    private func handleSegmentCompletion() {
        guard state == .playing || state == .paused else { return }
        loopDidComplete.send(loopIndex)

        if loopsRemaining > 0 {
            loopsRemaining -= 1
            loopIndex += 1
            currentTime = 0
            scheduleCurrentFile()
            if state == .playing {
                player.play()
            }
        } else {
            state = .finished
            currentTime = duration
            stopPositionTimer()
        }
    }

    private func startEngineIfNeeded() {
        if !engine.isRunning {
            do {
                try engine.start()
            } catch {
                // Engine start failure leaves state idle; UI shows transport disabled.
                state = .idle
            }
        }
    }

    // MARK: - Position polling

    private func startPositionTimer() {
        stopPositionTimer()
        let timer = Timer(timeInterval: 1.0 / 30.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.pollPosition()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        positionTimer = timer
    }

    private func stopPositionTimer() {
        positionTimer?.invalidate()
        positionTimer = nil
    }

    private func pollPosition() {
        guard let nodeTime = player.lastRenderTime,
              let playerTime = player.playerTime(forNodeTime: nodeTime),
              playerTime.sampleRate > 0
        else { return }
        currentTime = max(0, Double(playerTime.sampleTime) / playerTime.sampleRate)
    }

    // MARK: - Speed

    /// Applies playback speed (0.5–2.0) without changing pitch.
    func setSpeed(_ newSpeed: Float) {
        let clamped = max(0.5, min(newSpeed, 2.0))
        speed = clamped
        timePitch.rate = clamped
    }
}
