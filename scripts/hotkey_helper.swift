// hotkey_helper.swift — 全局快捷键监听（Carbon RegisterEventHotKey）
//
// 不需要辅助功能权限、不依赖宿主签名——RegisterEventHotKey 是系统级热键注册，
// 和"系统偏好设置→键盘→快捷键"同源，只要注册成功系统就回调。
//
// digital-life 的 perception daemon spawn 这个 helper：
//   - helper 注册全局热键 → 触发时输出 "TRIGGERED" 到 stdout
//   - daemon 读 stdout → toggle 录制
//   - helper 输出 "READY" 表示注册成功
//
// 用法: hotkey_helper <key> <modifiers>
//   key: 字母（z）或数字（1）
//   modifiers: cmd+shift / ctrl+alt / cmd 等
// 编译: swiftc -O hotkey_helper.swift -o hotkey_helper

import Carbon
import Cocoa

// ── 参数解析 ──
let args = CommandLine.arguments
guard args.count >= 3 else {
    fputs("usage: hotkey_helper <key> <modifiers>\n", stderr)
    exit(1)
}
let keyChar = args[1].lowercased()
let modStr = args[2].lowercased()

// key char → keycode（macOS 键码）
let keyMap: [String: UInt32] = [
    "a":0,"s":1,"d":2,"f":3,"h":4,"g":5,"z":6,"x":7,"c":8,"v":9,"b":11,
    "q":12,"w":13,"e":14,"r":15,"y":16,"t":17,"1":18,"2":19,"3":20,"4":21,
    "5":23,"6":22,"7":26,"8":28,"9":25,"0":29,"p":35,"o":31,"u":32,"i":34,
    "j":38,"k":40,"l":37,"n":45,"m":46,"space":49,
]
guard let keycode = keyMap[keyChar] else {
    fputs("unknown key: \(keyChar)\n", stderr); exit(1)
}

// modifiers → Carbon modifier flags
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

// ── HotKeyListener：用 @objc 方法接收 Carbon 事件回调 ──
class HotKeyListener {
    var triggeredCount = 0

    func onTriggered() {
        triggeredCount += 1
        print("TRIGGERED")
        fflush(stdout)
    }
}

let listener = HotKeyListener()

// ── 注册热键 + 安装 event handler ──
let eventSpec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                               eventKind: UInt32(kEventHotKeyPressed))

// 用 Unmanaged 传 self 指针给 C callback（避免 ARC 问题）
let listenerPtr = Unmanaged.passUnretained(listener).toOpaque()

// @convention(c) 闭包作为 Carbon event handler callback
let handlerCallback: EventHandlerUPP = { (_, _, userData) in
    guard let userData = userData else { return noErr }
    let l = Unmanaged<HotKeyListener>.fromOpaque(userData).takeUnretainedValue()
    l.onTriggered()
    return noErr
}

var handlerRef: EventHandlerRef? = nil
let installStatus = withUnsafePointer(to: eventSpec) { specPtr in
    InstallEventHandler(
        GetApplicationEventTarget(),
        handlerCallback,
        1,
        specPtr,
        listenerPtr,
        &handlerRef
    )
}

if installStatus != noErr {
    fputs("InstallEventHandler failed: \(installStatus)\n", stderr)
    exit(1)
}

// 注册热键
var hotKeyRef: EventHotKeyRef? = nil
var hotKeyID = EventHotKeyID(signature: OSType(0x5A434F44), id: 1)  // 'ZCOD'
let regStatus = RegisterEventHotKey(keycode, modifiers, hotKeyID,
                                     GetApplicationEventTarget(), 0, &hotKeyRef)
if regStatus != noErr {
    fputs("RegisterEventHotKey failed: \(regStatus)\n", stderr)
    exit(1)
}

print("READY \(keyChar)+\(modStr)")
fflush(stdout)

// ── 跑 NSApplication run loop（Carbon event 在这里派发）──
let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // 无 Dock 图标，但有 GUI session
app.run()
