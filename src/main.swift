import Cocoa
import WebKit
import UserNotifications
import Carbon.HIToolbox

// 置顶悬浮面板：加载任务面板页面（数据来自本地 server.py）
class PanelController: NSObject, NSWindowDelegate, WKScriptMessageHandler {
    let panel: NSPanel
    let webView: WKWebView
    let navDelegate: NavDelegate
    var chromeHost: ChromeHost?
    var pointerPolicy: PointerPolicy?

    override init() {
        let size = NSSize(width: 400, height: 640)
        panel = NSPanel(
            contentRect: NSRect(origin: NSPoint(x: 0, y: 0), size: size),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false
        )
        panel.minSize = NSSize(width: 340, height: 480)
        panel.title = "Agent 任务面板"
        panel.titlebarAppearsTransparent = true
        panel.titleVisibility = .hidden
        // 保留标准标题栏（红绿灯），但标题文字隐藏，视觉接近无边框
        panel.titleVisibility = .hidden
        panel.isMovableByWindowBackground = true
        panel.isFloatingPanel = false          // 置顶由页面里的 📌 开关控制
        panel.level = .normal
        // .stationary：切桌面时不跟着滑动画，任务面板应该钉在原位
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        panel.hidesOnDeactivate = false
        panel.isOpaque = false
        panel.backgroundColor = .clear

        // 先配好桥接再创建 WKWebView，确保页面识别为桌面壳（不抢 finished）
        let wkConfig = WKWebViewConfiguration()
        let ucc = wkConfig.userContentController
        let bridge = """
        window.agentPanel = {
          platform: 'macos',
          getSettings: function() { return window.agentPanel.preferenceRequest('get'); },
          setSettings: function(values) { return window.agentPanel.preferenceRequest('set', values); },
          preferenceRequest: function(action, values) {
            return new Promise(function(resolve, reject) {
              var id = String(Date.now()) + Math.random();
              var timer = setTimeout(function() { delete window.agentPanel.pending[id]; reject(new Error('原生操作响应超时')); }, 90000);
              window.agentPanel.pending[id] = function(value) { clearTimeout(timer); resolve(value); };
              window.webkit.messageHandlers.preferences.postMessage({action:action, values:values || {}, id:id});
            });
          },
          pending: {},
          resolvePreference: function(id, value) { var callback = this.pending[id]; delete this.pending[id]; if(callback) callback(value); },
          windowAction: function(a) {
            try { window.webkit.messageHandlers.windowAction.postMessage(String(a || '')); } catch (e) {}
          },
          openExternal: function(u) {
            return window.agentPanel.preferenceRequest('open', {url:String(u || '')});
          },
          notify: function() { /* 桌面壳由原生发通知与摘要卡 */ }
        };
        """
        ucc.addUserScript(WKUserScript(source: bridge, injectionTime: .atDocumentStart, forMainFrameOnly: true))

        webView = WKWebView(frame: NSRect(origin: .zero, size: size), configuration: wkConfig)
        webView.autoresizingMask = [.width, .height]
        webView.setValue(false, forKey: "drawsBackground")   // 透明背景
        panel.contentView?.addSubview(webView)
        // 透明模式下隐藏系统红绿灯（网页切模式时经 chrome 消息通道通知）
        chromeHost = ChromeHost(panel: panel, webView: webView)
        // 拖动条必须在 webView 之上才能收到鼠标事件；覆盖顶部 40px 整条留白区
        // 右上角 150px 让点击穿透到网页（透明模式下 📌⚙️◐↻ 悬浮按钮在该区域）
        let dragStrip = DragView(frame: NSRect(x: 0, y: size.height - 40, width: size.width, height: 40))
        dragStrip.autoresizingMask = [.width, .minYMargin]
        panel.contentView?.addSubview(dragStrip)

        // 指针策略：划过让路 / 停留接管（穿透能力就来自这里）
        pointerPolicy = PointerPolicy(panel: panel)

        navDelegate = NavDelegate()
        super.init()
        navDelegate.owner = self
        webView.navigationDelegate = navDelegate
        // message handler 的 target 必须是已完成 init 的 self
        for name in ["notify", "open", "settings", "chrome", "windowAction", "preferences"] {
            webView.configuration.userContentController.add(self, name: name)
        }

        panel.delegate = self
    }

    /// 首次启动：面板放在主屏可见区域右上角，紧挨菜单栏下方
    private func placeDefaultTopRight() {
        let size = panel.frame.size
        guard let screen = NSScreen.main ?? NSScreen.screens.first else {
            panel.center()
            return
        }
        let vf = screen.visibleFrame
        let margin: CGFloat = 12
        let origin = NSPoint(
            x: vf.maxX - size.width - margin,
            y: vf.maxY - size.height - margin
        )
        panel.setFrame(NSRect(origin: origin, size: size), display: false)
    }

    /// 恢复或写入默认窗口位置（须在 super.init 之后调用）
    func restoreOrPlaceFrame() {
        panel.setFrameAutosaveName("AgentTaskPanelFrame.v2")
        if !panel.setFrameUsingName("AgentTaskPanelFrame.v2") {
            placeDefaultTopRight()
        }
    }

    func load() {
        if let url = URL(string: "http://127.0.0.1:8765/") { _ = webView.load(URLRequest(url: url)) }
    }

    func show() {
        panel.orderFrontRegardless()
        NSApp.activate(ignoringOtherApps: true)
    }

    func showSettings() {
        show()
        webView.evaluateJavaScript("window.openPanelSettings?.()", completionHandler: nil)
    }

    func showReply(_ item: FinishBanner.Item) {
        let values = ["title": item.title, "agent": item.agent, "cwd": item.cwd,
                      "link": item.link, "reply_text": item.replyText]
        webView.callAsyncJavaScript("window.showPanelReply?.(reply)", arguments: ["reply": values],
                                    in: nil, in: .page, completionHandler: nil)
    }

    private func sendPreferences(requestID: String) {
        let values: [String: Any] = ["autostart": Settings.autostart,
            "autoYield": PointerPolicy.autoYield, "cursorJumpMode": Settings.cursorJumpMode.rawValue]
        guard let data = try? JSONSerialization.data(withJSONObject: values),
              let json = String(data: data, encoding: .utf8) else { return }
        guard let idData = try? JSONSerialization.data(withJSONObject: [requestID]),
              let idJSON = String(data: idData, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.agentPanel.resolvePreference(\(idJSON)[0], \(json))", completionHandler: nil)
    }

    // 主菜单「设置…」入口：showSettings 是内部方法，包一层 @objc 供菜单 action 调用
    @objc func showSettingsViaMenu() { showSettings() }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "preferences", let body = message.body as? [String: Any] {
            if body["action"] as? String == "open", let values = body["values"] as? [String: Any],
               let raw = values["url"] as? String, let url = URL(string: raw) {
                if let target = cursorAgentTarget(for: url) {
                    finishCursorRequest(body["id"] as? String, error: NSWorkspace.shared.open(target) ? nil : "未能打开 Cursor Agents 会话，请确认 Cursor 已安装。")
                    return
                }
                if url.scheme == "agentpanel", url.host == "cursor",
                   let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
                   parts.queryItems?.first(where: { $0.name == "mode" })?.value != "agents_only" {
                    openCursorConversation(url, requestID: body["id"] as? String ?? "")
                    return
                }
                let available = url.scheme == "agentpanel"
                    ? NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.todesktop.230313mzl4w4u92") != nil
                    : NSWorkspace.shared.urlForApplication(toOpen: url) != nil
                if available { openTaskLink(raw) }
                let id = body["id"] as? String ?? ""
                if let data = try? JSONSerialization.data(withJSONObject: [id]), let json = String(data: data, encoding: .utf8) {
                    webView.evaluateJavaScript("window.agentPanel.resolvePreference(\(json)[0], \(available ? "true" : "false"))", completionHandler: nil)
                }
                return
            }
            if body["action"] as? String == "set", let values = body["values"] as? [String: Any] {
                if let value = values["autostart"] as? Bool, value != Settings.autostart { Settings.autostart = value }
                if let value = values["autoYield"] as? Bool { PointerPolicy.autoYield = value; pointerPolicy?.refresh() }
                if let value = values["cursorJumpMode"] as? String, let mode = Settings.CursorJumpMode(rawValue: value) { Settings.cursorJumpMode = mode }
            }
            sendPreferences(requestID: body["id"] as? String ?? "")
            return
        }
        if message.name == "chrome" {
            chromeHost?.handle(message.body as? String)
            pointerPolicy?.refresh()
            return
        }
        if message.name == "settings" {
            showSettings()
            return
        }
        if message.name == "windowAction", let action = message.body as? String {
            switch action {
            case "minimize":
                panel.miniaturize(nil)
            case "close":
                // 有菜单栏时只隐藏，与点红灯一致
                if MenuBarManager.shared.isActive {
                    panel.orderOut(nil)
                } else {
                    panel.performClose(nil)
                }
            default:
                break
            }
            return
        }
        if message.name == "open", let s = message.body as? String {
            openTaskLink(s)
            return
        }
        // 桌面壳不应再走这条；留作兼容，避免双通道时与原生摘要卡重复
        guard message.name == "notify",
              let s = message.body as? String,
              let d = try? JSONSerialization.jsonObject(with: Data(s.utf8)) as? [String: String] else { return }
        Notify.post(title: d["title"] ?? "任务完成", body: d["body"] ?? "")
    }

    private func finishCursorRequest(_ requestID: String?, error: String?) {
        if let requestID {
            let result: [String: Any] = error.map { ["error": $0] } ?? ["ok": true]
            guard let args = try? JSONSerialization.data(withJSONObject: [requestID, result]),
                  let json = String(data: args, encoding: .utf8) else { return }
            webView.evaluateJavaScript("window.agentPanel.resolvePreference(...\(json))", completionHandler: nil)
        } else if let error {
            Notify.post(title: "Cursor 会话未打开", body: error)
        }
    }

    private func openCursorConversation(_ url: URL, requestID: String? = nil) {
        let params = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        guard let id = params.first(where: { $0.name == "id" })?.value, UUID(uuidString: id) != nil else {
            finishCursorRequest(requestID, error: "缺少有效的 Cursor 会话 ID，无法在 IDE 中定位。")
            return
        }
        let cwd = params.first(where: { $0.name == "cwd" })?.value ?? ""
        var isDirectory: ObjCBool = false
        guard cwd.hasPrefix("/"), FileManager.default.fileExists(atPath: cwd, isDirectory: &isDirectory), isDirectory.boolValue else {
            finishCursorRequest(requestID, error: "Cursor 工程路径已不可用，可从详情尝试打开 Agent 会话。")
            return
        }
        guard let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.todesktop.230313mzl4w4u92") else {
            finishCursorRequest(requestID, error: "未找到 Cursor 应用。")
            return
        }
        CursorBridgeInstaller.prepare(app: app) { [weak self] error in
            guard let self else { return }
            if let error { self.finishCursorRequest(requestID, error: error); return }
            self.dispatchCursorConversation(id: id, cwd: cwd, app: app, requestID: requestID)
        }
    }

    private func dispatchCursorConversation(id: String, cwd: String, app: URL, requestID: String?) {
        let folder = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".agent-panel-cursor-bridge")
        let token = UUID().uuidString.lowercased()
        let file = folder.appendingPathComponent(token + ".request.json")
        let ack = folder.appendingPathComponent(token + ".result.json")
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let data = try JSONSerialization.data(withJSONObject: ["requestID": token, "id": id, "cwd": cwd, "created": Date().timeIntervalSince1970])
            try data.write(to: file, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
            let process = Process()
            process.executableURL = app.appendingPathComponent("Contents/Resources/app/bin/cursor")
            process.arguments = ["--classic", cwd]
            try process.run()
        } catch {
            try? FileManager.default.removeItem(at: file)
            finishCursorRequest(requestID, error: "无法打开 Cursor IDE：\(error.localizedDescription)")
            return
        }
        let deadline = Date().addingTimeInterval(20)
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] timer in
            if let data = try? Data(contentsOf: ack),
               let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               result["requestID"] as? String == token {
                timer.invalidate()
                try? FileManager.default.removeItem(at: ack)
                self?.finishCursorRequest(requestID, error: result["ok"] as? Bool == true ? nil : (result["error"] as? String ?? "Cursor 未能打开会话。"))
            } else if Date() > deadline {
                timer.invalidate()
                try? FileManager.default.removeItem(at: file)
                self?.finishCursorRequest(requestID, error: "Cursor 桥接未响应。请确认工程已信任且桥接扩展已启用，然后重试。")
            }
        }
    }

    private func cursorAgentTarget(for url: URL) -> URL? {
        if url.scheme == "cursor", url.host == "anysphere.cursor-deeplink", url.path == "/agent" { return url }
        guard url.scheme == "agentpanel", url.host == "cursor",
              let params = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              (params.first(where: { $0.name == "cwd" })?.value ?? "").isEmpty,
              let id = params.first(where: { $0.name == "id" })?.value, UUID(uuidString: id) != nil else { return nil }
        var target = URLComponents()
        target.scheme = "cursor"
        target.host = "anysphere.cursor-deeplink"
        target.path = "/agent"
        target.queryItems = [URLQueryItem(name: "id", value: id)]
        return target.url
    }

    func openTaskLink(_ raw: String) {
        guard let url = URL(string: raw), let scheme = url.scheme?.lowercased() else { return }
        if let target = cursorAgentTarget(for: url) {
            finishCursorRequest(nil, error: NSWorkspace.shared.open(target) ? nil : "未能打开 Cursor Agents 会话，请确认 Cursor 已安装。")
            return
        }
        if scheme == "agentpanel", url.host?.lowercased() == "cursor" {
            let mode = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "mode" })?.value
            if mode == "agents_only" { openCursorInIDE(url) }
            else { openCursorConversation(url) }
            return
        }
        NSWorkspace.shared.open(url)
    }

    /// 打开 Cursor：按 Settings.cursorJumpMode 决定只开 IDE / 只开 Agents / 先 IDE 再 Agents。
    private func openCursorInIDE(_ url: URL) {
        let comps = URLComponents(url: url, resolvingAgainstBaseURL: false)
        let items = comps?.queryItems ?? []
        let cwd = items.first(where: { $0.name == "cwd" })?.value
        let composerId = items.first(where: { $0.name == "id" })?.value
        let requestedMode = items.first(where: { $0.name == "mode" })?.value
        let mode = requestedMode.flatMap(Settings.CursorJumpMode.init(rawValue:)) ?? Settings.cursorJumpMode

        let cursorBundleId = "com.todesktop.230313mzl4w4u92"
        let cursorApp = NSWorkspace.shared.urlForApplication(withBundleIdentifier: cursorBundleId)
        let cli = cursorApp?
            .appendingPathComponent("Contents/Resources/app/bin/cursor")

        func openProject() -> Bool {
            guard let cwd, !cwd.isEmpty, FileManager.default.fileExists(atPath: cwd) else { return false }
            if let cli, FileManager.default.isExecutableFile(atPath: cli.path) {
                let proc = Process()
                proc.executableURL = cli
                // Let Cursor select the existing window for this folder. -r can reuse
                // an unrelated active window when several projects are open.
                proc.arguments = ["--classic", cwd]
                do { try proc.run(); return true } catch { /* Fall through to AppKit. */ }
            }
            if let cursorApp {
                let folder = URL(fileURLWithPath: cwd)
                let config = NSWorkspace.OpenConfiguration()
                config.activates = true
                NSWorkspace.shared.open([folder], withApplicationAt: cursorApp, configuration: config)
                return true
            }
            return false
        }

        func openAgentsChat() -> Bool {
            guard let composerId, !composerId.isEmpty else { return false }
            var deeplink = URLComponents()
            deeplink.scheme = "cursor"
            deeplink.host = "anysphere.cursor-deeplink"
            deeplink.path = "/agent"
            deeplink.queryItems = [URLQueryItem(name: "id", value: composerId)]
            guard let target = deeplink.url else { return false }
            NSWorkspace.shared.open(target)
            return true
        }

        switch mode {
        case .ideOnly:
            if !openProject(), let cursorApp { NSWorkspace.shared.open(cursorApp) }
        case .agentsOnly:
            if !openAgentsChat() {
                if !openProject(), let cursorApp { NSWorkspace.shared.open(cursorApp) }
            }
        case .ideThenAgents:
            let opened = openProject()
            if let composerId, !composerId.isEmpty {
                var deeplink = URLComponents()
                deeplink.scheme = "cursor"
                deeplink.host = "anysphere.cursor-deeplink"
                deeplink.path = "/agent"
                deeplink.queryItems = [URLQueryItem(name: "id", value: composerId)]
                if let target = deeplink.url {
                    DispatchQueue.main.asyncAfter(deadline: .now() + (opened ? 0.7 : 0)) {
                        NSWorkspace.shared.open(target)
                    }
                    return
                }
            }
            if !opened, let cursorApp { NSWorkspace.shared.open(cursorApp) }
        }
    }

    func windowWillClose(_ notification: Notification) {
        // 有菜单栏常驻图标时关窗只隐藏面板，从菜单栏可再次打开；否则维持退出
        if MenuBarManager.shared.isActive {
            panel.orderOut(nil)
        } else {
            NSApp.terminate(nil)
        }
    }
}

// 菜单栏常驻图标：显示面板 / 置顶 / 自启 / 刷新 / 退出；执行中数量轮询显示在图标旁
class MenuBarManager: NSObject {
    static let shared = MenuBarManager()
    private var statusItem: NSStatusItem?
    private var timer: Timer?
    // 指针策略：菜单项与全局快捷键都操作它
    weak var pointerPolicy: PointerPolicy?
    private var hotKeyRef: EventHotKeyRef?

    var isActive: Bool { statusItem != nil }

    func start(panel: NSPanel) {
        guard statusItem == nil else { return }
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        // SF Symbol 模板图：跟随菜单栏深浅色自动着色，比 emoji 在深色菜单栏下干净
        let cfg = NSImage.SymbolConfiguration(pointSize: 14, weight: .medium)
        if let img = NSImage(systemSymbolName: "rectangle.stack.badge.person.crop", accessibilityDescription: "Agent 任务")?
            .withSymbolConfiguration(cfg) {
            img.isTemplate = true
            item.button?.image = img
        } else {
            item.button?.title = "🤖"
        }
        item.menu = buildMenu(panel: panel)
        statusItem = item
        registerHotKey()
        // 原生轮询 /api/tasks：角标 + 系统通知。
        // 不能只靠面板 WebView——收起/非关键时 JS 会被节流，finished 没人消费就会漏通知。
        // 2.5s：配合服务端 2～3 次确认，完成通知大约数秒内发出，而不是半分钟后
        timer = Timer.scheduledTimer(withTimeInterval: 2.5, repeats: true) { [weak self] _ in
            self?.refreshTasksAndNotify()
        }
        refreshTasksAndNotify()
    }

    private func buildMenu(panel: NSPanel) -> NSMenu {
        let menu = NSMenu()
        menu.delegate = self
        menu.autoenablesItems = false
        // 存引用供 menuNeedsUpdate 判断勾选状态
        menuPanel = panel
        return menu
    }

    weak var menuPanel: NSPanel?

    private static let agentNames: [String: String] = [
        "codex": "Codex", "claude": "Claude", "cursor": "Cursor",
        "qoder-cn": "Qoder CN", "deepseek": "DeepSeek", "dsh": "DSH",
        "cue": "Cue", "grok": "Grok", "today": "Today",
        "today-intl": "Today Intl", "today-cn": "Today CN",
    ]
    private static let finishKindLabels: [String: String] = [
        "completed": "回合结束", "blocked": "等待你处理", "failed": "任务失败",
        "heartbeat": "仍在执行",
    ]
    private func refreshTasksAndNotify() {
        URLSession.shared.dataTask(with: URL(string: "http://127.0.0.1:8765/api/tasks")!) { [weak self] data, _, _ in
            guard let data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            let tasks = obj["tasks"] as? [[String: Any]] ?? []
            let finished = obj["finished"] as? [[String: Any]] ?? []
            let heartbeats = obj["heartbeats"] as? [[String: Any]] ?? []
            let running = tasks.filter {
                ($0["status"] as? String) == "running"
                    && !(($0["recent"] as? String) ?? "").hasPrefix("应用在线")
            }.count
            DispatchQueue.main.async {
                self?.updateStatusIcon(running: running)
                let prefs = obj["notifications"] as? [String: Bool] ?? [:]
                Settings.notificationPreferences = prefs
                let events = (finished + heartbeats).filter {
                    let kind = $0["kind"] as? String ?? "completed"
                    return prefs[kind] ?? (kind != "heartbeat")
                }
                guard !events.isEmpty else { return }
                let completions = events.filter { ($0["kind"] as? String) == "completed" }
                if completions.count > 1 {
                    Notify.post(title: "\(completions.count) 个任务回合结束", body: completions.prefix(3).compactMap { $0["title"] as? String }.joined(separator: " · "))
                }
                for f in events {
                    let agent = f["agent"] as? String ?? ""
                    let kind = f["kind"] as? String ?? "completed"
                    let agentName = Self.agentNames[agent] ?? agent
                    let kindLabel = Self.finishKindLabels[kind] ?? kind
                    let title = f["title"] as? String ?? ""
                    let recent = f["recent"] as? String ?? ""
                    if kind != "completed" || completions.count < 2 {
                        Notify.post(title: "\(agentName) \(kindLabel)", body: title)
                    }
                    if Settings.attentionOnFinish && (kind != "completed" || completions.count < 2) {
                        FinishBanner.shared.enqueue(FinishBanner.Item(
                            agent: agentName,
                            kindLabel: kindLabel,
                            kind: kind,
                            title: title,
                            recent: recent,
                            replyText: f["reply_text"] as? String ?? "",
                            cwd: f["cwd"] as? String ?? "",
                            link: f["link"] as? String ?? "",
                            taskID: f["task_id"] as? String ?? "",
                            revision: f["revision"] as? String ?? ""
                        ))
                    }
                }
                if Settings.attentionOnFinish && completions.count > 1 {
                    FinishBanner.shared.enqueue(FinishBanner.Item(
                        agent: "Agent", kindLabel: "回合结束", kind: "completed",
                        title: "\(completions.count) 个任务回合结束",
                        recent: completions.prefix(4).compactMap { $0["title"] as? String }.joined(separator: " · "),
                        replyText: "", cwd: "", link: "", taskID: "", revision: ""
                    ))
                }
                if Settings.attentionOnFinish, Settings.focusOnFinish, events.contains(where: { ($0["kind"] as? String) != "heartbeat" }) {
                    NSApp.requestUserAttention(.informationalRequest)
                }
            }
        }.resume()
    }

    private func updateStatusIcon(running: Int) {
        guard let btn = statusItem?.button else { return }
        let cfg = NSImage.SymbolConfiguration(pointSize: 14, weight: .medium)
        if let img = NSImage(systemSymbolName: "rectangle.stack.badge.person.crop", accessibilityDescription: "Agent 任务")?
            .withSymbolConfiguration(cfg) {
            img.isTemplate = true
            btn.image = img
        }
        btn.title = running > 0 ? " \(running)" : ""
    }
}

extension MenuBarManager: NSMenuDelegate {
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let pinned = FileManager.default.fileExists(atPath: NSString(string: "~/.agent-panel-pin").expandingTildeInPath)
        let items: [(String, Selector?, NSControl.StateValue)] = [
            ("显示任务面板", #selector(showPanel), .off),
            ("让面板可操作（⌥⌘A）", #selector(toggleInteractive), pointerPolicy?.mode == .active ? .on : .off),
            ("置顶窗口", #selector(togglePin), pinned ? .on : .off),
            ("任务完成时弹摘要卡", #selector(toggleAttentionOnFinish), Settings.attentionOnFinish ? .on : .off),
            ("自动让路（不挡其他应用）", #selector(toggleAutoYield), PointerPolicy.autoYield ? .on : .off),
            ("开机自启", #selector(toggleAutostart), Settings.autostart ? .on : .off),
            ("刷新数据", #selector(refreshData), .off),
            ("退出", #selector(NSApplication.terminate(_:)), .off),
        ]
        for (idx, entry) in items.enumerated() {
            if idx == items.count - 1 { menu.addItem(.separator()) }
            let it = NSMenuItem(title: entry.0, action: entry.1, keyEquivalent: "")
            it.target = self
            it.state = entry.2
            it.isEnabled = true
            menu.addItem(it)
            if entry.0 == "任务完成时弹摘要卡" {
                let jump = NSMenuItem(title: "Cursor 跳转方式", action: nil, keyEquivalent: "")
                let sub = NSMenu()
                for mode in Settings.CursorJumpMode.allCases {
                    let mi = NSMenuItem(title: mode.menuTitle, action: #selector(setCursorJumpMode(_:)), keyEquivalent: "")
                    mi.target = self
                    mi.representedObject = mode.rawValue
                    mi.state = Settings.cursorJumpMode == mode ? .on : .off
                    sub.addItem(mi)
                }
                jump.submenu = sub
                menu.addItem(jump)
            }
        }
    }
    /// 恢复面板：处理最小化、orderOut 隐藏、被其他窗挡住三种情况
    func revealPanel() {
        guard let panel = menuPanel else { return }
        if panel.isMiniaturized {
            panel.deminiaturize(nil)
        }
        panel.makeKeyAndOrderFront(nil)
        panel.orderFrontRegardless()
        NSApp.activate(ignoringOtherApps: true)
        // 唤起时可操作，避免「看得见点不了」
        pointerPolicy?.activate()
    }

    @objc func showPanel() {
        revealPanel()
    }
    @objc func togglePin() {
        let flag = NSString(string: "~/.agent-panel-pin").expandingTildeInPath
        if FileManager.default.fileExists(atPath: flag) {
            try? FileManager.default.removeItem(atPath: flag)
        } else {
            FileManager.default.createFile(atPath: flag, contents: nil)
        }
    }
    // 自动让路开关：关掉后窗口恢复常驻可交互
    @objc func toggleAutoYield() {
        PointerPolicy.autoYield.toggle()
        pointerPolicy?.refresh()
    }
    // 菜单栏切换「可操作」状态：面板从穿透态恢复成可点击
    @objc func toggleInteractive() {
        pointerPolicy?.toggle()
    }
    @objc func toggleAttentionOnFinish() { Settings.attentionOnFinish.toggle() }
    @objc func setCursorJumpMode(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String,
              let mode = Settings.CursorJumpMode(rawValue: raw) else { return }
        Settings.cursorJumpMode = mode
    }
    @objc func toggleAutostart() { Settings.autostart.toggle() }
    @objc func refreshData() {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        proc.arguments = ["-f", "AgentTaskPanel.app/Contents/Resources/server.py"]
        try? proc.run()
    }

    // 全局快捷键 ⌥⌘A：不用离开当前 App 就能把面板切成可操作
    private func registerHotKey() {
        // 用 Carbon 的 RegisterEventHotKey：系统级生效、不需要辅助功能权限
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, userData in
            guard let userData else { return noErr }
            let mgr = Unmanaged<MenuBarManager>.fromOpaque(userData).takeUnretainedValue()
            DispatchQueue.main.async { mgr.pointerPolicy?.toggle() }
            return noErr
        }, 1, &spec, Unmanaged.passUnretained(self).toOpaque(), nil)

        let hotKeyID = EventHotKeyID(signature: OSType(0x4154504C), id: 1)  // 'ATPL'
        // ⌥⌘A = optionKey | cmdKey + 'A'(0x00)
        RegisterEventHotKey(UInt32(kVK_ANSI_A), UInt32(optionKey | cmdKey),
                            hotKeyID, GetApplicationEventTarget(), 0, &hotKeyRef)
    }

    @objc func openGitHub() {
        NSWorkspace.shared.open(URL(string: "https://github.com/jack19941210/agent-task-panel")!)
    }

    /// 菜单栏图标区分「可操作 / 穿透」两态，用户扫一眼就知道现在面板吃不吃点击
    func refreshStatusIcon(interactive: Bool) {
        guard let btn = statusItem?.button else { return }
        let name = interactive ? "rectangle.stack.badge.person.crop.fill" : "rectangle.stack.badge.person.crop"
        let cfg = NSImage.SymbolConfiguration(pointSize: 14, weight: .medium)
        if let img = NSImage(systemSymbolName: name, accessibilityDescription: "Agent 任务")?
            .withSymbolConfiguration(cfg) {
            img.isTemplate = true
            btn.image = img
        }
        btn.alphaValue = interactive ? 1 : 0.65
    }

}

// 指针策略：面板默认「不挡事」，需要用时再明确唤起
//
// 设计目标（用户诉求）：一直置顶会影响浏览其他页面，所以默认不能吃点击。
//
// 状态：
//   passthrough  默认态。整窗穿透，点击直接落到下面的 App；面板保持可见
//   armed        鼠标划过面板时短暂「预备」：面板提亮一点作为可点击的提示，
//                但仍然穿透，所以误划过去不会打断正在做的事
//   active       用户明确唤起（点击菜单栏 / 全局快捷键 / 点面板上的唤起提示）后
//                才恢复交互，此时才真正吃点击
//
// 恢复为 passthrough 的三种方式：
//   1. 鼠标离开面板并超过 lingerDelay（自动让路，不需要任何操作）
//   2. 菜单栏「让面板可操作」再点一次
//   3. 全局快捷键 ⌥⌘A 切换
//
// 关键：ignoresMouseEvents 是整窗开关，设 true 后窗口收不到任何鼠标事件，
// 自己的 NSTrackingArea 也会一起失效，所以必须靠窗口外的全局监听判断指针位置。
// 官方文档：addGlobalMonitorForEvents 只观察事件、不吞事件；
// 鼠标类事件不需要辅助功能权限（只有键盘事件需要）。
class PointerPolicy {
    enum Mode { case passthrough, armed, active }

    // 鼠标离开面板后多久自动回到穿透态
    static let lingerDelay: TimeInterval = 1.2
    // 划过时的透明度：几乎隐去，给下面内容让路；移开 0.5 秒后自动恢复可见
    static let passingAlpha: CGFloat = 0.12

    // 菜单栏开关：默认关闭（常驻可点）；打开后划过会淡出并穿透，避免挡其他窗口
    static var autoYield: Bool {
        get { UserDefaults.standard.object(forKey: "autoYield") as? Bool ?? false }
        set { UserDefaults.standard.set(newValue, forKey: "autoYield") }
    }

    private weak var panel: NSPanel?
    private var monitors: [Any] = []
    private var leaveTimer: Timer?
    private(set) var mode: Mode = .passthrough

    // 状态变化回调：给菜单栏同步勾选状态用
    var onModeChange: ((Mode) -> Void)?

    init(panel: NSPanel) {
        self.panel = panel
        // 默认可交互；若用户开了「自动让路」再由 refresh()/sync() 切回穿透
        if Self.autoYield {
            panel.ignoresMouseEvents = true
            mode = .passthrough
        } else {
            panel.ignoresMouseEvents = false
            mode = .active
        }
        installMonitors()
    }

    deinit {
        monitors.forEach(NSEvent.removeMonitor)
        leaveTimer?.invalidate()
    }

    // MARK: - 监听器

    private func installMonitors() {
        let events: NSEvent.EventTypeMask = [
            .mouseMoved, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged,
        ]
        // 全局：鼠标在别的 App 上移动时也要知道，否则面板无法感知指针离开
        if let g = NSEvent.addGlobalMonitorForEvents(matching: events, handler: { [weak self] _ in
            // 回调在后台线程，窗口属性必须回主线程改
            DispatchQueue.main.async { self?.sync() }
        }) {
            monitors.append(g)
        }
        // 本地：鼠标已经落在本 App 窗口上时全局监听收不到（Apple 文档明确说明）
        if let l = NSEvent.addLocalMonitorForEvents(matching: events, handler: { [weak self] event in
            self?.sync()
            return event
        }) {
            monitors.append(l)
        }
    }

    // MARK: - 对外接口

    /// 明确唤起交互：菜单栏 / 全局快捷键 / 面板内的唤起按钮
    func activate() {
        DispatchQueue.main.async { [weak self] in
            guard let self, let panel = self.panel else { return }
            // 先切状态再改窗口属性：反过来会有一段「状态还是 passthrough、
            // 但穿透已关」的窗口期，期间若有鼠标事件进来，sync() 会按
            // passthrough 分支把穿透又设回 true，导致唤起间歇性失效
            self.setMode(.active)
            self.applyActive(panel)
            // 不调 orderFront：面板本来就在屏幕上，只是吃不吃点击的区别。
            // 而且非激活状态下 orderFront 可能被系统忽略，多一个不确定因素。
            // 唤起时指针可能已经在面板外（用快捷键时很常见）。
            // 不能只依赖后续 mouseMoved 事件启动让路计时：鼠标静止时
            // 系统不会产生移动事件，面板会一直停在可交互态吃点击。
            if !panel.frame.contains(NSEvent.mouseLocation) {
                self.startLeaveTimer()
            } else {
                self.cancelLeaveTimer()
            }
            // 兜底：窗口属性偶尔会被系统异步回写，100ms 后再校准一次
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
                guard let self, self.mode == .active, let panel = self.panel else { return }
                self.applyActive(panel)
            }
        }
    }

    /// 立即回到穿透态（菜单栏 / 快捷键再点一次）
    func deactivate() {
        DispatchQueue.main.async { [weak self] in
            guard let self, let panel = self.panel else { return }
            self.setMode(.passthrough)
            self.cancelLeaveTimer()
            self.applyPassthrough(panel)
        }
    }

    func toggle() {
        if mode == .active { deactivate() } else { activate() }
    }

    /// 外部触发一次状态校准（启动、切开关、切背景模式时调用）
    func refresh() {
        DispatchQueue.main.async { [weak self] in self?.sync() }
    }

    // MARK: - 状态机

    private func sync() {
        guard let panel, panel.isVisible, !panel.isMiniaturized else { return }

        // 关掉「自动让路」：常驻可交互、不透明（老行为）
        guard Self.autoYield else {
            cancelLeaveTimer()
            setMode(.active)
            if panel.ignoresMouseEvents { panel.ignoresMouseEvents = false }
            if panel.alphaValue != 1 { panel.alphaValue = 1 }
            return
        }

        // 按住鼠标（拖窗口 / 拖选）期间不改状态，否则拖到一半窗口会「松手」
        if NSEvent.pressedMouseButtons != 0 { return }

        let inside = panel.frame.contains(NSEvent.mouseLocation)

        switch mode {
        case .active:
            // 已唤起：鼠标离开一段时间后自动让路
            applyActive(panel)
            if inside {
                cancelLeaveTimer()
            } else if leaveTimer == nil {
                startLeaveTimer()
            }

        case .passthrough:
            // 默认穿透且可见。鼠标划过时继续让路：淡出，彻底不挡下面内容
            if inside {
                setMode(.armed)
                panel.alphaValue = Self.passingAlpha
                startPassRestore()
            } else {
                applyPassthrough(panel)
            }

        case .armed:
            if inside {
                // 保持淡出，等恢复计时到期或鼠标离开
                if panel.alphaValue != Self.passingAlpha { panel.alphaValue = Self.passingAlpha }
            } else {
                // 划过去就恢复可见，但始终保持穿透
                cancelLeaveTimer()
                setMode(.passthrough)
                panel.alphaValue = 1
            }
        }
    }

    /// 划过时启动的恢复计时：鼠标停在面板上不动也不该一直隐身
    private func startPassRestore() {
        guard leaveTimer == nil else { return }
        let timer = Timer(timeInterval: Self.lingerDelay, repeats: false) { [weak self] _ in
            guard let self, let panel = self.panel else { return }
            self.leaveTimer = nil
            // 仍停在面板上：恢复可见（但继续穿透，想要交互请按 ⌥⌘A）
            panel.alphaValue = 1
        }
        RunLoop.main.add(timer, forMode: .common)
        leaveTimer = timer
    }

    private func startLeaveTimer() {
        leaveTimer?.invalidate()
        let timer = Timer(timeInterval: Self.lingerDelay, repeats: false) { [weak self] _ in
            guard let self, let panel = self.panel else { return }
            self.leaveTimer = nil
            // 到期再确认一次指针确实在面板外
            guard !panel.frame.contains(NSEvent.mouseLocation) else { return }
            self.setMode(.passthrough)
            self.applyPassthrough(panel)
        }
        // 必须用 .common：鼠标悬停/拖动时 runloop 处于 eventTracking 模式，
        // 默认模式的 Timer 不会触发（实测会导致状态卡死）
        RunLoop.main.add(timer, forMode: .common)
        leaveTimer = timer
    }

    private func cancelLeaveTimer() {
        leaveTimer?.invalidate()
        leaveTimer = nil
    }

    /// active 态的唯一写入点：可交互 + 完全显现
    private func applyActive(_ panel: NSPanel) {
        if panel.ignoresMouseEvents { panel.ignoresMouseEvents = false }
        if panel.alphaValue != 1 { panel.alphaValue = 1 }
    }

    /// passthrough 态的唯一写入点：穿透 + 完全显现
    private func applyPassthrough(_ panel: NSPanel) {
        if !panel.ignoresMouseEvents { panel.ignoresMouseEvents = true }
        if panel.alphaValue != 1 { panel.alphaValue = 1 }
    }

    private func setMode(_ m: Mode) {
        guard mode != m else { return }
        mode = m
        onModeChange?(m)
    }
}

// 红绿灯显隐控制：透明模式全隐藏，鼠标移入面板时短暂浮现
// 继承 NSResponder 才能作为 NSTrackingArea owner 接收 mouseEntered/Exited
class ChromeHost: NSResponder {
    weak var panel: NSPanel?
    weak var webView: WKWebView?
    var tracking = false

    init(panel: NSPanel, webView: WKWebView) {
        super.init()
        self.panel = panel
        self.webView = webView
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func handle(_ payload: String?) {
        let hide = payload == "clear"
        DispatchQueue.main.async {
            guard let sv = self.panel?.standardWindowButton(.closeButton)?.superview else { return }
            sv.isHidden = hide
            if hide {
                self.startHoverTracking()
            } else {
                self.stopHoverTracking()
            }
        }
    }

    // 用 NSTrackingArea 监听鼠标进出面板，进出瞬间闪烁红绿灯 400ms 提供可见反馈
    func startHoverTracking() {
        guard !tracking, let content = panel?.contentView else { return }
        tracking = true
        content.addTrackingArea(NSTrackingArea(
            rect: NSRect(origin: .zero, size: content.bounds.size),
            options: [.mouseEnteredAndExited, .activeInKeyWindow, .inVisibleRect],
            owner: self))
        if let chrome = panel?.standardWindowButton(.closeButton)?.superview {
            chrome.alphaValue = 0
        }
    }

    func stopHoverTracking() {
        tracking = false
        panel?.contentView?.trackingAreas.forEach { panel?.contentView?.removeTrackingArea($0) }
        if let chrome = panel?.standardWindowButton(.closeButton)?.superview {
            chrome.isHidden = false
            chrome.alphaValue = 1
        }
    }

    override func mouseEntered(with event: NSEvent) {
        guard let chrome = panel?.standardWindowButton(.closeButton)?.superview else { return }
        chrome.isHidden = false
        chrome.alphaValue = 0.9
        NSObject.cancelPreviousPerformRequests(withTarget: self, selector: #selector(fadeOut), object: nil)
        perform(#selector(fadeOut), with: nil, afterDelay: 2.5)
    }

    override func mouseExited(with event: NSEvent) {
        NSObject.cancelPreviousPerformRequests(withTarget: self, selector: #selector(fadeOut), object: nil)
        fadeOut()
    }

    @objc func fadeOut() {
        panel?.standardWindowButton(.closeButton)?.superview?.alphaValue = 0
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
            if self?.panel?.standardWindowButton(.closeButton)?.superview?.alphaValue == 0 {
                self?.panel?.standardWindowButton(.closeButton)?.superview?.isHidden = true
            }
        }
    }
}

class NavDelegate: NSObject, WKNavigationDelegate {
    weak var owner: PanelController?

    // 页面里的 deep link（targetFrame == nil 的新窗口跳转）交给系统打开
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        // 同帧导航的 codex:// 等自定义 scheme 也拦截：交给系统打开，避免 WebView 自身导航挂掉
        if let url = navigationAction.request.url,
           !["http", "https", "about", "file"].contains(url.scheme?.lowercased() ?? "") {
            // agentpanel://cursor：交给面板控制器打开 Cursor IDE
            if url.scheme?.lowercased() == "agentpanel" {
                owner?.openTaskLink(url.absoluteString)
                decisionHandler(.cancel)
                return
            }
            // today://?bundle=…：多个 App 抢同一 scheme 时用 bundle id 精确路由
            if let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
               let bid = comps.queryItems?.first(where: { $0.name == "bundle" })?.value {
                var clean = URLComponents(url: url, resolvingAgainstBaseURL: false)!
                clean.queryItems = nil
                if let target = URL(string: clean.string ?? url.absoluteString) {
                    if let appURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bid) {
                        let config = NSWorkspace.OpenConfiguration()
                        // open(_:withApplicationAt:) 把 URL 作为待打开资源交给指定 App，
                        // App 收到的是正常的 URL 事件（等价于 open -a <app> <url>）
                        NSWorkspace.shared.open([target], withApplicationAt: appURL,
                                                configuration: config) { _, _ in }
                    } else {
                        NSWorkspace.shared.open(target)
                    }
                    decisionHandler(.cancel)
                    return
                }
            }
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }
}

// 顶部拖动条：捕获鼠标按下后交给窗口拖动
class DragView: NSView {
    // 顶部右侧按钮区宽度：该范围内不拦截，事件透传给 WebView
    static let passThroughWidth: CGFloat = 150
    override func hitTest(_ p: NSPoint) -> NSView? {
        guard let pLocal = superview.map({ convert(p, from: $0) }) else { return super.hitTest(p) }
        // 必须先判断点是否在本视图 frame 内，否则会吞掉下方统计卡/任务卡的点击
        guard bounds.contains(pLocal) else { return nil }
        if pLocal.x > bounds.width - DragView.passThroughWidth { return nil }
        return self
    }
    override func mouseDown(with event: NSEvent) {
        window?.performDrag(with: event)
    }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

// 轮询 pin 标志文件，切换置顶层级
class PinWatcher {
    var timer: Timer?
    let panel: NSPanel
    let flag = NSString(string: "~/.agent-panel-pin").expandingTildeInPath

    init(panel: NSPanel) {
        self.panel = panel
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            let pinned = FileManager.default.fileExists(atPath: self.flag)
            let want = pinned ? NSWindow.Level.floating : NSWindow.Level.normal
            if self.panel.level != want {
                self.panel.level = want
                if pinned { self.panel.orderFrontRegardless() }
            }
        }
    }
}

// server.py 的 FNV-1a 指纹，与 Python 侧 build_id() 同算法。
// 用来识别端口上跑的是不是当前这份代码——重装后旧进程还占着 8765 时，
// 只探"端口通不通"会让面板静默显示旧数据。
func serverFileBuildID() -> String {
    guard let py = Bundle.main.url(forResource: "server", withExtension: "py"),
          let data = try? Data(contentsOf: py) else { return "0" }
    var h: UInt64 = 0xcbf29ce484222325
    for byte in data {
        h = (h ^ UInt64(byte)) &* 0x100000001b3
    }
    return String(format: "%016llx", h)
}

// 探测端口上的服务并返回 (是否响应, 对端 build id)
func probeServer() -> (ok: Bool, build: String) {
    let semaphore = DispatchSemaphore(value: 0)
    var ok = false
    var build = ""
    URLSession.shared.dataTask(with: URL(string: "http://127.0.0.1:8765/api/health")!) { data, r, _ in
        if (r as? HTTPURLResponse)?.statusCode == 200, let data,
           let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            ok = true
            build = (j["build"] as? String) ?? ""
        }
        semaphore.signal()
    }.resume()
    _ = semaphore.wait(timeout: .now() + 1)
    return (ok, build)
}

// 端口上是否有任何本项目的服务在应答（兼容 0.3.2 及更早版本：没有 /api/health）
func legacyServerResponding() -> Bool {
    let semaphore = DispatchSemaphore(value: 0)
    var ok = false
    URLSession.shared.dataTask(with: URL(string: "http://127.0.0.1:8765/api/tasks")!) { _, r, _ in
        ok = (r as? HTTPURLResponse)?.statusCode == 200
        semaphore.signal()
    }.resume()
    _ = semaphore.wait(timeout: .now() + 1)
    return ok
}

// 杀掉旧的本地数据服务。
// 优先用 pidfile；老版本不写 pidfile，则回退到"查监听 8765 的进程"，
// 且只杀命令行里确实是本项目 server.py 的进程，避免误伤别的服务。
func killStaleServer() {
    let pidPath = NSHomeDirectory() + "/.agent-panel-server.pid"
    var killed = false
    if let raw = try? String(contentsOfFile: pidPath, encoding: .utf8),
       let pid = Int32(raw.trimmingCharacters(in: .whitespacesAndNewlines)), pid > 0 {
        // PID 可能已被系统复用：确认命令行确实是本项目的 server.py 再杀
        if let cmd = try? runProcess("/bin/ps", ["-o", "command=", "-p", "\(pid)"]),
           cmd.contains("AgentTaskPanel.app/Contents/Resources/server.py") {
            killed = kill(pid, SIGTERM) == 0
        }
    }
    if !killed, let out = try? runProcess("/usr/sbin/lsof", ["-ti", "tcp:8765"]) {
        for line in out.split(separator: "\n") {
            guard let pid = Int32(line.trimmingCharacters(in: .whitespaces)) else { continue }
            guard let cmd = try? runProcess("/bin/ps", ["-o", "command=", "-p", "\(pid)"]),
                  cmd.contains("AgentTaskPanel.app/Contents/Resources/server.py") else { continue }
            kill(pid, SIGTERM)
        }
    }
    usleep(400_000)
}

func runProcess(_ path: String, _ args: [String]) throws -> String {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: path)
    proc.arguments = args
    let pipe = Pipe()
    proc.standardOutput = pipe
    try proc.run()
    proc.waitUntilExit()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    return String(data: data, encoding: .utf8) ?? ""
}

// 拉起本地数据服务（bundle 内的 server.py），已运行则跳过
func startServer() {
    let mine = serverFileBuildID()
    let probe = probeServer()
    if probe.ok {
        if probe.build == mine { return }
        // 端口上是旧版本的服务：先清掉，否则面板会一直显示旧数据
        killStaleServer()
    } else if legacyServerResponding() {
        // 老版本没有 /api/health，但确实在应答：同样要清掉
        killStaleServer()
    }
    guard let py = Bundle.main.url(forResource: "server", withExtension: "py") else { return }
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    proc.arguments = [py.path, "--build", mine]
    try? proc.run()
}

// 数据服务守护：server.py 是 App 子进程，一旦意外退出面板就会一直"未响应"。
// 每 5 秒健康检查，失败就重新拉起（拉起前先探活，避免服务忙时重复起进程）。
class ServerKeeper {
    var timer: Timer?
    var restarting = false

    static func probe() -> Bool {
        let p = probeServer()
        return p.ok && p.build == serverFileBuildID()
    }

    static func launch() {
        guard let py = Bundle.main.url(forResource: "server", withExtension: "py") else { return }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        proc.arguments = [py.path, "--build", serverFileBuildID()]
        try? proc.run()
    }

    func start() {
        timer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            guard let self, !self.restarting else { return }
            if ServerKeeper.probe() { return }
            self.restarting = true
            // 端口上可能是旧版本的进程：先清理再拉起，避免永远探测失败循环
            if probeServer().ok || legacyServerResponding() { killStaleServer() }
            ServerKeeper.launch()
            // 给服务 3 秒就绪窗口，期间跳过检查避免重复拉起
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
                self?.restarting = false
            }
        }
    }
}


// ============ 设置项（defaults 持久化 + Dock 菜单） ============
let DOMAIN = "local.agent-task-panel"
enum Settings {
    static var autostart: Bool {
        // 注意：不能用 UserDefaults(suiteName:) 传自己的 bundle id，系统会忽略并告警。
        // 直接用 standard，它会落到 ~/Library/Preferences/<bundleid>.plist
        get { UserDefaults.standard.bool(forKey: "autostart") }
        set {
            UserDefaults.standard.set(newValue, forKey: "autostart")
            let agentPath = NSHomeDirectory() + "/Library/LaunchAgents/local.agent-task-panel.plist"
            let fm = FileManager.default
            if newValue {
                let plist: [String: Any] = [
                    "Label": "local.agent-task-panel",
                    "ProgramArguments": ["/usr/bin/open", "-a", "/Applications/AgentTaskPanel.app"],
                    "RunAtLoad": true
                ]
                let dir = (agentPath as NSString).deletingLastPathComponent
                try? fm.createDirectory(atPath: dir, withIntermediateDirectories: true)
                let data = try? PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
                try? data?.write(to: URL(fileURLWithPath: agentPath))
                let proc = Process()
                proc.executableURL = URL(fileURLWithPath: "/bin/launchctl")
                proc.arguments = ["load", agentPath]
                try? proc.run()
            } else {
                try? fm.removeItem(atPath: agentPath)
                let proc = Process()
                proc.executableURL = URL(fileURLWithPath: "/bin/launchctl")
                proc.arguments = ["unload", agentPath]
                try? proc.run()
            }
        }
    }

    /// 任务完成/堵塞/失败时：弹出单任务摘要卡 + Dock 弹跳。默认开。
    static var notificationPreferences: [String: Bool] = [:]
    static var focusOnFinish: Bool { notificationPreferences["focus"] ?? true }
    static var attentionOnFinish: Bool {
        get { notificationPreferences["summary"] ?? true }
        set {
            notificationPreferences["summary"] = newValue
            var req = URLRequest(url: URL(string: "http://127.0.0.1:8765/api/config")!)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["notifications": ["summary": newValue]])
            URLSession.shared.dataTask(with: req).resume()
        }
    }

    enum CursorJumpMode: String, CaseIterable {
        case ideThenAgents = "ide_then_agents"
        case ideOnly = "ide_only"
        case agentsOnly = "agents_only"
        var menuTitle: String {
            switch self {
            case .ideThenAgents: return "先 IDE 再 Agents 会话"
            case .ideOnly: return "只打开 IDE 工程"
            case .agentsOnly: return "只打开 Agents 会话"
            }
        }
    }

    static var cursorJumpMode: CursorJumpMode {
        get {
            let raw = UserDefaults.standard.string(forKey: "cursorJumpMode") ?? ""
            return CursorJumpMode(rawValue: raw) ?? .ideOnly
        }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: "cursorJumpMode") }
    }
}

// Dock 菜单：勾选项读写设置
class DockMenuDelegate: NSObject {
    let autostartItem = NSMenuItem(title: "开机自启", action: #selector(toggleAutostart), keyEquivalent: "")
}
var dockMenuDelegate = DockMenuDelegate()

extension DockMenuDelegate: NSApplicationDelegate, NSMenuDelegate {
    /// Dock 图标点击：系统在「无可见窗口」时回调这里。
    /// 面板最小化或关闭隐藏后必须在此 deminiaturize / orderFront，否则只能靠菜单栏「显示任务面板」。
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        MenuBarManager.shared.revealPanel()
        return true
    }

    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let menu = NSMenu()
        menu.delegate = self
        return menu
    }
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let show = NSMenuItem(title: "显示任务面板", action: #selector(showPanelFromDock), keyEquivalent: "")
        show.target = self
        menu.addItem(show)
        menu.addItem(.separator())
        let a = NSMenuItem(title: "开机自启", action: #selector(toggleAutostart), keyEquivalent: "")
        a.target = self
        a.state = Settings.autostart ? .on : .off
        menu.addItem(a)
        let b = NSMenuItem(title: "刷新数据", action: #selector(refreshData), keyEquivalent: "r")
        b.target = self
        menu.addItem(b)
        menu.addItem(.separator())
        let q = NSMenuItem(title: "退出", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(q)
    }
    @objc func showPanelFromDock() {
        MenuBarManager.shared.revealPanel()
    }
    @objc func toggleAutostart() { Settings.autostart.toggle() }
    @objc func refreshData() {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        proc.arguments = ["-f", "AgentTaskPanel.app/Contents/Resources/server.py"]
        try? proc.run()
    }
}

/// 可成为 key 的无边框面板，否则摘要卡按钮点不到、有时也不上屏
final class AttentionPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

/// 任务终态摘要弹卡：只展示当前这条任务的结果与最近进展，不把整块看板抢到前台。
final class FinishBanner: NSObject {
    static let shared = FinishBanner()

    struct Item {
        let agent: String
        let kindLabel: String
        let kind: String
        let title: String
        let recent: String
        let replyText: String
        let cwd: String
        let link: String
        let taskID: String
        let revision: String
    }

    var onOpenLink: ((String) -> Void)?
    var onShowBoard: (() -> Void)?
    var onShowReply: ((Item) -> Void)?

    private var queue: [Item] = []
    private var panel: NSPanel?
    private var autoDismiss: DispatchWorkItem?
    private weak var titleLabel: NSTextField?
    private weak var recentLabel: NSTextField?
    private weak var boardButton: NSButton?
    private weak var metaLabel: NSTextField?
    private weak var kindLabel: NSTextField?
    private var current: Item?

    func enqueue(_ item: Item) {
        queue.append(item)
        if current == nil {
            showNext()
        } else {
            // 已有弹卡：刷新角标提示队列长度
            refreshChrome()
        }
    }

    private func showNext() {
        autoDismiss?.cancel()
        guard !queue.isEmpty else {
            hide()
            return
        }
        current = queue.removeFirst()
        guard let item = current else { return }
        let p = ensurePanel()
        kindLabel?.stringValue = "\(item.agent) · \(item.kindLabel)"
        kindLabel?.textColor = kindColor(item.kind)
        titleLabel?.stringValue = item.title
        let summary = item.recent.isEmpty ? "（暂无进展摘要，可点「打开对话」查看详情）" : item.recent
        recentLabel?.stringValue = summary
        boardButton?.title = item.replyText.isEmpty ? "打开看板" : "查看全文"
        let cwdShort = shortCwd(item.cwd)
        let pending = queue.isEmpty ? "" : "  ·  还有 \(queue.count) 条"
        metaLabel?.stringValue = (cwdShort.isEmpty ? "" : cwdShort) + pending
        refreshChrome()

        applyKindChrome(item.kind)
        placeTopRight(p)
        p.level = .statusBar
        p.orderFrontRegardless()
        if Settings.focusOnFinish && item.kind != "heartbeat" {
            p.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }

        let work = DispatchWorkItem { [weak self] in self?.dismissCurrent() }
        autoDismiss = work
        // 堵塞/失败多留一会儿；心跳短一点
        let secs: TimeInterval = (item.kind == "blocked" || item.kind == "failed") ? 18
            : (item.kind == "heartbeat" ? 8 : 12)
        DispatchQueue.main.asyncAfter(deadline: .now() + secs, execute: work)
    }

    private func refreshChrome() {
        guard let item = current else { return }
        let cwd = shortCwd(item.cwd)
        metaLabel?.stringValue = cwd + (queue.isEmpty ? "" : "  ·  还有 \(queue.count) 条")
    }

    private weak var accentBar: NSView?
    private weak var rootView: NSView?

    private func applyKindChrome(_ kind: String) {
        accentBar?.layer?.backgroundColor = kindColor(kind).cgColor
        let bg: NSColor
        switch kind {
        case "failed": bg = NSColor(calibratedRed: 0.22, green: 0.08, blue: 0.08, alpha: 0.97)
        case "blocked": bg = NSColor(calibratedRed: 0.22, green: 0.14, blue: 0.05, alpha: 0.97)
        case "heartbeat": bg = NSColor(calibratedRed: 0.06, green: 0.14, blue: 0.18, alpha: 0.97)
        default: bg = NSColor(calibratedWhite: 0.12, alpha: 0.96)
        }
        rootView?.layer?.backgroundColor = bg.cgColor
    }

    private func dismissCurrent() {
        autoDismiss?.cancel()
        current = nil
        if queue.isEmpty {
            hide()
        } else {
            showNext()
        }
    }

    private func hide() {
        autoDismiss?.cancel()
        panel?.orderOut(nil)
        current = nil
    }

    @objc private func tapDismiss() { dismissCurrent() }
    @objc private func tapOpen() {
        if let link = current?.link, !link.isEmpty {
            onOpenLink?(link)
        } else {
            onShowBoard?()
        }
        dismissCurrent()
    }
    @objc private func tapBoard() {
        if let item = current, !item.replyText.isEmpty, let onShowReply {
            onShowReply(item)
        } else {
            onShowBoard?()
        }
        dismissCurrent()
    }
    @objc private func tapRemind() {
        guard let item = current else { return }
        if item.taskID.isEmpty { onShowBoard?(); dismissCurrent(); return }
        dismissCurrent()
        // 5 分钟后再弹同一条摘要，避免忙别的事时错过
        DispatchQueue.main.asyncAfter(deadline: .now() + 300) {
            URLSession.shared.dataTask(with: URL(string: "http://127.0.0.1:8765/api/tasks?view=1")!) { data, _, _ in
                guard let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let tasks = obj["tasks"] as? [[String: Any]],
                      let task = tasks.first(where: { ($0["task_id"] as? String) == item.taskID }) else { return }
                let expected = ["completed": "done", "blocked": "waiting", "failed": "failed", "heartbeat": "running"][item.kind]
                guard (task["display_status"] as? String ?? task["status"] as? String) == expected,
                      !item.revision.isEmpty else { return }
                let complete = task["complete_at"] as? String ?? ""
                let recentAt = task["recent_at"] as? String ?? ""
                let mtime = (task["mtime"] as? NSNumber)?.doubleValue ?? 0
                let currentRevision = !complete.isEmpty ? complete : (!recentAt.isEmpty ? recentAt : String(mtime))
                guard currentRevision == item.revision else { return }
                let prefs = obj["notifications"] as? [String: Bool] ?? [:]
                guard prefs[item.kind] ?? (item.kind != "heartbeat") else { return }
                DispatchQueue.main.async {
                    Settings.notificationPreferences = prefs
                    Notify.post(title: "\(item.agent) 提醒", body: item.title)
                    if Settings.attentionOnFinish { FinishBanner.shared.enqueue(item) }
                }
            }.resume()
        }
    }

    private func ensurePanel() -> NSPanel {
        if let panel { return panel }
        // 加宽：中文按钮「打开对话/打开看板/稍后提醒」在 72pt 会被截成「打开…」
        let width: CGFloat = 448
        let height: CGFloat = 210
        let p = AttentionPanel(
            contentRect: NSRect(x: 0, y: 0, width: width, height: height),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered, defer: false
        )
        p.isFloatingPanel = true
        p.becomesKeyOnlyIfNeeded = false
        p.level = .statusBar
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        p.hidesOnDeactivate = false
        p.isMovableByWindowBackground = true
        p.backgroundColor = .clear
        p.isOpaque = false
        p.hasShadow = true

        let root = NSView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor(calibratedWhite: 0.12, alpha: 0.96).cgColor
        root.layer?.cornerRadius = 12
        root.layer?.masksToBounds = true
        p.contentView = root
        rootView = root

        let bar = NSView(frame: NSRect(x: 0, y: 0, width: 4, height: height))
        bar.wantsLayer = true
        bar.layer?.backgroundColor = NSColor.systemGreen.cgColor
        bar.autoresizingMask = [.height]
        root.addSubview(bar)
        accentBar = bar

        let kind = makeLabel(font: .systemFont(ofSize: 12, weight: .semibold), color: .systemGreen)
        kind.frame = NSRect(x: 16, y: height - 36, width: width - 32, height: 18)
        root.addSubview(kind)
        kindLabel = kind

        let title = makeLabel(font: .systemFont(ofSize: 15, weight: .bold), color: .white)
        title.frame = NSRect(x: 16, y: height - 64, width: width - 32, height: 22)
        title.maximumNumberOfLines = 1
        title.lineBreakMode = .byTruncatingTail
        root.addSubview(title)
        titleLabel = title

        let recent = makeLabel(font: .systemFont(ofSize: 13), color: NSColor(calibratedWhite: 0.85, alpha: 1))
        recent.frame = NSRect(x: 16, y: 48, width: width - 32, height: 78)
        recent.maximumNumberOfLines = 4
        recent.cell?.wraps = true
        recent.lineBreakMode = .byWordWrapping
        root.addSubview(recent)
        recentLabel = recent

        let meta = makeLabel(font: .systemFont(ofSize: 11), color: NSColor(calibratedWhite: 0.55, alpha: 1))
        meta.frame = NSRect(x: 16, y: 34, width: width - 32, height: 14)
        root.addSubview(meta)
        metaLabel = meta

        let openBtn = NSButton(title: "打开对话", target: self, action: #selector(tapOpen))
        openBtn.bezelStyle = .rounded
        openBtn.controlSize = .small
        openBtn.frame = NSRect(x: 16, y: 10, width: 88, height: 24)
        root.addSubview(openBtn)

        let boardBtn = NSButton(title: "打开看板", target: self, action: #selector(tapBoard))
        boardBtn.bezelStyle = .rounded
        boardBtn.controlSize = .small
        boardBtn.frame = NSRect(x: 108, y: 10, width: 88, height: 24)
        root.addSubview(boardBtn)
        boardButton = boardBtn

        let remindBtn = NSButton(title: "稍后提醒", target: self, action: #selector(tapRemind))
        remindBtn.bezelStyle = .rounded
        remindBtn.controlSize = .small
        remindBtn.frame = NSRect(x: 200, y: 10, width: 88, height: 24)
        root.addSubview(remindBtn)

        let okBtn = NSButton(title: "知道了", target: self, action: #selector(tapDismiss))
        okBtn.bezelStyle = .rounded
        okBtn.controlSize = .small
        okBtn.frame = NSRect(x: width - 98, y: 10, width: 82, height: 24)
        okBtn.keyEquivalent = "\r"
        root.addSubview(okBtn)

        panel = p
        return p
    }

    private func makeLabel(font: NSFont, color: NSColor) -> NSTextField {
        let t = NSTextField(labelWithString: "")
        t.font = font
        t.textColor = color
        t.backgroundColor = .clear
        t.isBordered = false
        t.isEditable = false
        t.isSelectable = false
        return t
    }

    private func kindColor(_ kind: String) -> NSColor {
        switch kind {
        case "failed": return .systemRed
        case "blocked": return .systemOrange
        default: return .systemGreen
        }
    }

    private func shortCwd(_ cwd: String) -> String {
        guard !cwd.isEmpty else { return "" }
        let parts = cwd.split(separator: "/").filter { !$0.isEmpty }
        return parts.suffix(2).joined(separator: "/")
    }

    private func placeTopRight(_ p: NSPanel) {
        guard let screen = NSScreen.main ?? NSScreen.screens.first else { return }
        let vf = screen.visibleFrame
        let size = p.frame.size
        p.setFrameOrigin(NSPoint(x: vf.maxX - size.width - 16, y: vf.maxY - size.height - 16))
    }
}

// 系统通知：前台也要出横幅（默认前台 App 会吞掉通知）
enum Notify {
    static let center = UNUserNotificationCenter.current()

    static func post(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
    }
}

final class NotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        if #available(macOS 11.0, *) {
            completionHandler([.banner, .sound, .list])
        } else {
            completionHandler([.alert, .sound])
        }
    }
}

let notificationDelegate = NotificationDelegate()

let app = NSApplication.shared
app.setActivationPolicy(.regular)
app.delegate = dockMenuDelegate

// ============ 应用主菜单（左上角 App 名菜单）：macOS 标准结构 ============
// NSApp 的 mainMenu 不设置时，App 菜单只有灰默认项；补齐 关于/设置/隐藏/退出 与系统快捷键
func buildAppMainMenu(panel: NSPanel, controller: PanelController) -> NSMenu {
    let main = NSMenu()

    // App 菜单（第一列，标题由系统自动取 CFBundleName）
    let appMenu = NSMenu()
    let about = NSMenuItem(title: "关于 AgentTaskPanel", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
    appMenu.addItem(about)
    appMenu.addItem(.separator())
    let settings = NSMenuItem(title: "设置…", action: #selector(PanelController.showSettingsViaMenu), keyEquivalent: ",")
    settings.target = controller
    appMenu.addItem(settings)
    let refresh = NSMenuItem(title: "刷新数据服务", action: #selector(DockMenuDelegate.refreshData), keyEquivalent: "r")
    refresh.target = dockMenuDelegate
    appMenu.addItem(refresh)
    appMenu.addItem(.separator())
    appMenu.addItem(NSMenuItem(title: "隐藏 AgentTaskPanel", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h"))
    let hideOthers = NSMenuItem(title: "隐藏其他", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
    hideOthers.keyEquivalentModifierMask = [.command, .option]
    appMenu.addItem(hideOthers)
    appMenu.addItem(NSMenuItem(title: "显示全部", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: ""))
    appMenu.addItem(.separator())
    appMenu.addItem(NSMenuItem(title: "退出 AgentTaskPanel", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
    let appItem = NSMenuItem()
    appItem.submenu = appMenu
    main.addItem(appItem)

    // 窗口菜单
    let windowMenu = NSMenu(title: "窗口")
    let showPanel = NSMenuItem(title: "显示任务面板", action: #selector(MenuBarManager.showPanel), keyEquivalent: "m")
    showPanel.target = MenuBarManager.shared
    windowMenu.addItem(showPanel)
    let close = NSMenuItem(title: "关闭面板", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
    windowMenu.addItem(close)
    let winItem = NSMenuItem()
    winItem.submenu = windowMenu
    main.addItem(winItem)

    // 帮助菜单
    let helpMenu = NSMenu(title: "帮助")
    let help = NSMenuItem(title: "GitHub 仓库", action: #selector(MenuBarManager.openGitHub), keyEquivalent: "")
    help.target = MenuBarManager.shared
    helpMenu.addItem(help)
    let helpItem = NSMenuItem()
    helpItem.submenu = helpMenu
    main.addItem(helpItem)

    return main
}
startServer()
Notify.center.delegate = notificationDelegate
Notify.center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
let controller = PanelController()
controller.restoreOrPlaceFrame()
controller.load()
controller.show()
FinishBanner.shared.onOpenLink = { [weak controller] link in
    controller?.openTaskLink(link)
}
FinishBanner.shared.onShowBoard = {
    MenuBarManager.shared.revealPanel()
}
FinishBanner.shared.onShowReply = { [weak controller] item in
    MenuBarManager.shared.revealPanel()
    controller?.showReply(item)
}
app.mainMenu = buildAppMainMenu(panel: controller.panel, controller: controller)
// 必须强持有 watcher，否则实例被释放、定时器回调里 self 为 nil，置顶永远不生效
let pinWatcher = PinWatcher(panel: controller.panel)
let serverKeeper = ServerKeeper()
serverKeeper.start()
MenuBarManager.shared.start(panel: controller.panel)
MenuBarManager.shared.pointerPolicy = controller.pointerPolicy
// 状态变化时刷新菜单栏图标的可操作提示
controller.pointerPolicy?.onModeChange = { mode in
    MenuBarManager.shared.refreshStatusIcon(interactive: mode == .active)
}
// 启动即校准：尊重「自动让路」开关；默认关闭时保持可点
controller.pointerPolicy?.refresh()
MenuBarManager.shared.refreshStatusIcon(
    interactive: controller.pointerPolicy?.mode == .active
)
app.run()
