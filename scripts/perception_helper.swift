// perception_helper.swift — 独立感知 helper（不依赖宿主签名/权限）
//
// 一个独立编译的 Swift 二进制，负责：
//   1. 注册全局快捷键（Carbon RegisterEventHotKey，不需要辅助功能）
//   2. 按下时截图（ScreenCaptureKit/CGDisplay，独立权限路径）
//   3. 按下时录音（AVAudioEngine，独立权限路径）
//   4. 再按时停止，输出帧路径 + 音频路径到 stdout
//
// digital-life 的 daemon spawn 这个 helper，读 stdout 获取结果。
// 权限绑在 helper 二进制自身，不依赖 python 宿主或终端 app。
//
// 用法: perception_helper <key> <modifiers> <out_dir> [max_seconds] [fps]
//   key: 字母（z）
//   modifiers: cmd+shift
//   out_dir: 截图/音频输出目录
//   max_seconds: 最大录制时长（默认 120）
//   fps: 抽帧帧率（默认 2）
//
// 编译: swiftc -O perception_helper.swift -o perception_helper -framework Carbon -framework AVFoundation -framework CoreGraphics

import Carbon
import Cocoa
import AVFoundation
import CoreGraphics
import ImageIO
import ScreenCaptureKit

// ── 参数（从环境变量读，因为 open --args 传参不可靠）──
let keyChar = ProcessInfo.processInfo.environment["PERCEPTION_KEY"] ?? "z"
let modStr = ProcessInfo.processInfo.environment["PERCEPTION_MODS"] ?? "cmd+shift"
let outDir = ProcessInfo.processInfo.environment["PERCEPTION_OUT_DIR"] ?? "/tmp/perception_capture"
let maxSeconds = Int(ProcessInfo.processInfo.environment["PERCEPTION_MAX_SEC"] ?? "120") ?? 120
let fps = Double(ProcessInfo.processInfo.environment["PERCEPTION_FPS"] ?? "2.0") ?? 2.0

// 创建输出目录
try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)

// key char → keycode
let keyMap: [String: UInt32] = [
    "a":0,"s":1,"d":2,"f":3,"h":4,"g":5,"z":6,"x":7,"c":8,"v":9,"b":11,
    "q":12,"w":13,"e":14,"r":15,"y":16,"t":17,"1":18,"2":19,"3":20,"4":21,
    "5":23,"6":22,"7":26,"8":28,"9":25,"0":29,"p":35,"space":49,
]
guard let keycode = keyMap[keyChar] else {
    fputs("unknown key: \(keyChar)\n", stderr); exit(1)
}

var modifiers: UInt32 = 0
for m in modStr.split(separator: "+") {
    switch m.trimmingCharacters(in: .whitespaces) {
    case "cmd","command": modifiers |= UInt32(cmdKey)
    case "shift": modifiers |= UInt32(shiftKey)
    case "ctrl","control": modifiers |= UInt32(controlKey)
    case "alt","option": modifiers |= UInt32(optionKey)
    default: break
    }
}

// ── 感知控制器 ──
class PerceptionController {
    let outDir: String
    let maxSeconds: Int
    let fps: Double
    var isRecording = false
    var framePaths: [String] = []
    var audioPath: String? = nil
    private var captureTimer: DispatchSourceTimer?
    private var startTime: Date = Date()
    private var audioEngine: AVAudioEngine?
    private var audioFile: AVAudioFile?
    private var frameIdx = 0

    init(outDir: String, maxSeconds: Int, fps: Double) {
        self.outDir = outDir
        self.maxSeconds = maxSeconds
        self.fps = fps
    }

    func toggle() {
        if isRecording {
            stop()
        } else {
            start()
        }
    }

    func start() {
        isRecording = true
        framePaths = []
        audioPath = nil
        frameIdx = 0
        startTime = Date()
        print("STARTED")
        fflush(stdout)

        // 截图定时器
        let interval = 1.0 / fps
        let queue = DispatchQueue.global(qos: .userInitiated)
        captureTimer = DispatchSource.makeTimerSource(queue: queue)
        captureTimer?.schedule(deadline: .now(), repeating: interval)
        captureTimer?.setEventHandler { [weak self] in
            self?.captureFrame()
            // 超时检查
            if let s = self, Date().timeIntervalSince(s.startTime) > Double(s.maxSeconds) {
                DispatchQueue.main.async { s.stop() }
            }
        }
        captureTimer?.resume()

        // 录音
        startAudioCapture()
    }

    func stop() {
        guard isRecording else { return }
        isRecording = false
        captureTimer?.cancel()
        captureTimer = nil

        // 停录音
        audioEngine?.stop()
        audioEngine = nil
        audioFile = nil

        let duration = Date().timeIntervalSince(startTime)
        print("STOPPED \(duration)s frames=\(framePaths.count) audio=\(audioPath ?? "none")")
        fflush(stdout)

        // 输出帧路径（每行一个，供 python 读）
        for p in framePaths {
            print("FRAME \(p)")
        }
        if let ap = audioPath {
            print("AUDIO \(ap)")
        }
        print("DONE")
        fflush(stdout)
    }

    private func captureFrame() {
        guard isRecording else { return }
        let idx = frameIdx
        let path = "\(outDir)/frame_\(String(format: "%04d", idx)).png"

        // ScreenCaptureKit 需要先获取 SCShareableContent → 创建 filter → 截图
        SCShareableContent.getExcludingDesktopWindows(false,
            onScreenWindowsOnly: true) { content, error in
            guard let content = content, let display = content.displays.first else { return }
            let filter = SCContentFilter(display: display,
                                          excludingWindows: [])
            let config = SCStreamConfiguration()
            config.width = display.width
            config.height = display.height

            SCScreenshotManager.captureImage(contentFilter: filter,
                configuration: config) { image, error in
                guard let image = image else { return }
                if let dest = CGImageDestinationCreateWithURL(
                    URL(fileURLWithPath: path) as CFURL, "public.png" as CFString, 1, nil) {
                    CGImageDestinationAddImage(dest, image, nil)
                    if CGImageDestinationFinalize(dest) {
                        self.framePaths.append(path)
                        self.frameIdx += 1
                    }
                }
            }
        }
    }

    private func startAudioCapture() {
        let engine = AVAudioEngine()
        let inputNode = engine.inputNode
        let format = inputNode.outputFormat(forBus: 0)
        let ts = Int(Date().timeIntervalSince1970)
        let path = "\(outDir)/audio_\(ts).caf"
        audioPath = path

        do {
            audioFile = try AVAudioFile(forWriting: URL(fileURLWithPath: path),
                                         settings: format.settings)
        } catch {
            fputs("audio file create failed: \(error)\n", stderr)
            return
        }

        inputNode.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buf, _ in
            guard let self = self, let file = self.audioFile else { return }
            do { try file.write(from: buf) } catch {}
        }

        do {
            try engine.start()
            audioEngine = engine
        } catch {
            fputs("audio engine start failed: \(error)\n", stderr)
            audioPath = nil
        }
    }
}

let controller = PerceptionController(outDir: outDir, maxSeconds: maxSeconds, fps: fps)
let controllerPtr = Unmanaged.passUnretained(controller).toOpaque()

// ── 注册热键 ──
let eventSpec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                               eventKind: UInt32(kEventHotKeyPressed))

let handlerCallback: EventHandlerUPP = { (_, _, userData) in
    guard let userData = userData else { return noErr }
    let c = Unmanaged<PerceptionController>.fromOpaque(userData).takeUnretainedValue()
    c.toggle()
    return noErr
}

var handlerRef: EventHandlerRef? = nil
let _ = withUnsafePointer(to: eventSpec) { specPtr in
    InstallEventHandler(GetApplicationEventTarget(), handlerCallback, 1, specPtr, controllerPtr, &handlerRef)
}

var hotKeyRef: EventHotKeyRef? = nil
var hotKeyID = EventHotKeyID(signature: OSType(0x5A434F44), id: 1)
let _ = RegisterEventHotKey(keycode, modifiers, hotKeyID, GetApplicationEventTarget(), 0, &hotKeyRef)

print("READY \(keyChar)+\(modStr)")
fflush(stdout)

// 跑 app run loop
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.run()
