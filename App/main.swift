// Atelier NX : fenêtre Mac qui lance server.py (Resources) et affiche sa page locale.
import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?
    var baseURL: URL?
    var token: String?

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        NSApp.appearance = NSAppearance(named: .darkAqua)   // interface sombre, comme la page
        let cfg = WKWebViewConfiguration()
        web = WKWebView(frame: .zero, configuration: cfg)
        web.uiDelegate = self
        web.navigationDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 780),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Atelier NX"
        window.backgroundColor = NSColor(calibratedRed: 0.09, green: 0.09, blue: 0.10, alpha: 1)
        web.setValue(false, forKey: "drawsBackground")   // pas d'éclair blanc avant le chargement de la page
        window.contentView = web
        window.setFrameAutosaveName("AtelierNX")
        window.center()
        window.makeKeyAndOrderFront(nil)
        startServer()
    }

    func pythonPath() -> String {
        let support = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/AtelierNX/config.json")
        if let data = try? Data(contentsOf: support),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let p = obj["python"] as? String, FileManager.default.isExecutableFile(atPath: p) { return p }
        let candidates = [NSHomeDirectory() + "/Downloads/Archive/nxnative-venv/bin/python",
                          "/opt/homebrew/bin/python3.14", "/opt/homebrew/bin/python3.13", "/opt/homebrew/bin/python3"]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) } ?? "/usr/bin/python3"
    }

    /// Un serveur Atelier NX déjà lancé (par une autre fenêtre ou à la main) : on l'affiche au lieu d'en démarrer un second.
    func runningServer() -> URL? {
        let f = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/AtelierNX/server.json")
        guard let data = try? Data(contentsOf: f),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let port = obj["port"] as? Int, let url = URL(string: "http://127.0.0.1:\(port)/") else { return nil }
        var req = URLRequest(url: url, timeoutInterval: 1.5)
        req.httpMethod = "GET"
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: req) { d, r, _ in
            ok = (r as? HTTPURLResponse)?.statusCode == 200 && (d.flatMap { String(data: $0, encoding: .utf8) }?.contains("Atelier NX") ?? false)
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2)
        return ok ? url : nil
    }

    func startServer() {
        if let url = runningServer() {
            baseURL = url
            web.load(URLRequest(url: url))
            return
        }
        guard let script = Bundle.main.path(forResource: "server", ofType: "py") else { fail("server.py manquant dans l'app."); return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath())
        p.arguments = ["-I", script]
        let out = Pipe()
        p.standardOutput = out
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                if self?.baseURL == nil { self?.fail("Le serveur s'est arrêté (code \(proc.terminationStatus)).") }
            }
        }
        do { try p.run() } catch { fail("Impossible de lancer Python : \(error.localizedDescription)"); return }
        server = p
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            guard let s = String(data: h.availableData, encoding: .utf8), let r = s.range(of: #"PORT (\d+)"#, options: .regularExpression) else { return }
            let port = s[r].dropFirst(5)
            h.readabilityHandler = nil
            DispatchQueue.main.async {
                guard let self, let url = URL(string: "http://127.0.0.1:\(port)/") else { return }
                self.baseURL = url
                self.web.load(URLRequest(url: url))
            }
        }
    }

    func fail(_ msg: String) {
        web.loadHTMLString("<body style='font:15px -apple-system;padding:40px;color:#ff6b6b;background:#17171a'>\(msg)</body>", baseURL: nil)
    }

    // Le jeton est dans la page : on le relit pour interroger /api/busy à la fermeture.
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        webView.evaluateJavaScript("TOKEN") { [weak self] v, _ in self?.token = v as? String }
    }

    func buildBusy() -> Bool {
        guard let base = baseURL, let token, let url = URL(string: "api/busy", relativeTo: base) else { return false }
        var req = URLRequest(url: url, timeoutInterval: 2)
        req.setValue(token, forHTTPHeaderField: "X-Token")
        let sem = DispatchSemaphore(value: 0)
        var busy = false
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { busy = obj["busy"] as? Bool ?? false }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 3)
        return busy
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if buildBusy() {
            let a = NSAlert()
            a.messageText = "Un build est en cours"
            a.informativeText = "Quitter arrête le build. Le prochain reprendra ce qui est déjà extrait."
            a.addButton(withTitle: "Continuer le build")
            a.addButton(withTitle: "Quitter quand même")
            if a.runModal() == .alertFirstButtonReturn { return .terminateCancel }
        }
        return .terminateNow
    }

    func applicationWillTerminate(_ note: Notification) {
        if let s = server, s.isRunning { s.terminate(); s.waitUntilExit() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    // confirm() / alert() de la page
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = message
        a.addButton(withTitle: "OK"); a.addButton(withTitle: "Annuler")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = message; a.runModal(); completionHandler()
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Recharger", action: #selector(reload), keyEquivalent: "r")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Masquer Atelier NX", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quitter Atelier NX", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Édition")
        edit.addItem(withTitle: "Couper", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copier", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Coller", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Tout sélectionner", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Fenêtre")
        win.addItem(withTitle: "Fermer", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        win.addItem(withTitle: "Réduire", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        winItem.submenu = win
        NSApp.mainMenu = main
    }

    @objc func reload() { web.reload() }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
