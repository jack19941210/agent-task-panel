import Foundation

/// Installs only the bundled bridge through Cursor's supported extension CLI.
/// Runs off the UI thread; requests are queued until installation and verification finish.
enum CursorBridgeInstaller {
    private static let queue = DispatchQueue(label: "agent-panel.cursor-bridge-install")

    static func prepare(app: URL, completion: @escaping (String?) -> Void) {
        queue.async {
            let error = ensureInstalled(
                cli: app.appendingPathComponent("Contents/Resources/app/bin/cursor"),
                resources: Bundle.main.resourceURL
            )
            DispatchQueue.main.async { completion(error) }
        }
    }

    static func ensureInstalled(cli: URL, resources: URL?) -> String? {
        guard let resources,
              let data = try? Data(contentsOf: resources.appendingPathComponent("cursor-bridge-package.json")),
              let manifest = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let publisher = manifest["publisher"] as? String,
              let name = manifest["name"] as? String,
              let version = manifest["version"] as? String else {
            return "安装包缺少 Cursor 会话组件，请重新安装最新版面板。"
        }
        let vsix = resources.appendingPathComponent("agent-panel-bridge.vsix")
        guard FileManager.default.fileExists(atPath: vsix.path) else {
            return "安装包缺少 Cursor 会话组件，请重新安装最新版面板。"
        }
        let id = publisher + "." + name
        func installed() -> Bool? {
            guard let output = run(cli, ["--list-extensions", "--show-versions"], timeout: 8) else { return nil }
            return output.split(whereSeparator: \.isNewline).contains { line in
                let parts = line.trimmingCharacters(in: .whitespaces).split(separator: "@")
                return parts.count == 2 && parts[0].lowercased() == id.lowercased()
                    && String(parts[1]).compare(version, options: .numeric) != .orderedAscending
            }
        }
        guard let exists = installed() else {
            return "无法检查 Cursor 组件，请确认 Cursor 可正常启动后重试。"
        }
        if exists { return nil }
        guard run(cli, ["--install-extension", vsix.path, "--force"], timeout: 45) != nil else {
            return "Cursor 会话组件自动安装失败或超时，请检查 Cursor 的扩展安装限制后重试。"
        }
        guard installed() == true else {
            return "Cursor 会话组件未安装成功，请检查扩展策略后重试。"
        }
        return nil
    }

    private static func run(_ executable: URL, _ arguments: [String], timeout: TimeInterval) -> String? {
        // A file avoids pipe-buffer deadlocks for verbose CLI output.
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        guard FileManager.default.createFile(atPath: file.path, contents: nil, attributes: [.posixPermissions: 0o600]),
              let output = try? FileHandle(forWritingTo: file) else { return nil }
        defer { try? output.close(); try? FileManager.default.removeItem(at: file) }
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        let ended = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in ended.signal() }
        do { try process.run() } catch { return nil }
        guard ended.wait(timeout: .now() + timeout) == .success else {
            if process.isRunning { process.terminate() }
            return nil
        }
        guard process.terminationStatus == 0 else { return nil }
        return try? String(contentsOf: file, encoding: .utf8)
    }
}
