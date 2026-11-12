// SelfUpdate.swift — met l'app à jour depuis ses releases GitHub.
//
// Le même fichier vit dans Biblifilm, Biblijeu, Atelier NX et Recomps : une
// correction faite ici se reporte dans les trois autres.
//
// Au lancement (au plus une fois par jour, sauf si la recherche automatique
// est désactivée dans les réglages), l'app lit les releases de son dépôt et
// garde la plus récente qui contient un zip Mac (Mac et Linux partagent les
// releases mais n'ont pas les mêmes numéros). Si elle est plus récente que
// l'app en place, elle propose de l'installer : le zip
// est téléchargé, décompressé, vérifié, puis remplace l'app, qui se relance.
// Un fichier téléchargé par l'app elle-même ne porte pas la quarantaine, donc
// macOS ne redemande pas « Ouvrir quand même ».
//
// Vérifications avant de remplacer quoi que ce soit :
//  - même identifiant de bundle et version réellement plus récente ;
//  - signature valide, et si l'app en place est signée avec un certificat,
//    la nouvelle doit satisfaire la même exigence de signature (même
//    certificat) : une release qui n'aurait pas été signée par ce Mac de
//    build est refusée.
import AppKit
import Security

enum SelfUpdate {

    struct Release {
        let version: String
        let notes: String
        let page: URL
        let download: URL
        let size: Int
    }

    enum Failure: LocalizedError {
        case network(String), noZip, noApp, wrongApp, notNewer, signature(String),
             unzip, notWritable, translocated

        var errorDescription: String? {
            switch self {
            case .network(let why): return "GitHub n'a pas répondu correctement (\(why))."
            case .noZip:            return "Aucune release ne contient encore de version Mac."
            case .noApp:            return "Le fichier téléchargé ne contient pas d'application."
            case .wrongApp:         return "Le fichier téléchargé contient une autre application."
            case .notNewer:         return "Le fichier téléchargé n'est pas plus récent que la version installée."
            case .signature(let why): return "La signature de la nouvelle version n'est pas celle attendue (\(why))."
            case .unzip:            return "Impossible de décompresser le fichier téléchargé."
            case .notWritable:      return "Impossible de remplacer l'app à son emplacement actuel (droits insuffisants)."
            case .translocated:     return "L'app est lancée depuis un emplacement temporaire de macOS. Glissez-la dans le dossier Applications, relancez-la, puis réessayez."
            }
        }
    }

    // MARK: - Lancement et menu

    private static var repo = ""
    private static var busy = false
    private static let lastCheckKey = "SelfUpdate.lastCheck"
    private static let skippedKey = "SelfUpdate.skippedVersion"
    /// Clé du réglage « Rechercher automatiquement les mises à jour » (vrai
    /// par défaut). Exposée pour `@AppStorage` dans les vues de réglages.
    static let automaticKey = "SelfUpdate.automatic"

    /// Recherche automatique au lancement. Désactivée, seule l'entrée de menu
    /// « Rechercher les mises à jour… » interroge GitHub.
    static var automatic: Bool {
        get { UserDefaults.standard.object(forKey: automaticKey) as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: automaticKey) }
    }

    /// La version écartée par « Ignorer cette version », s'il y en a une.
    /// Elle n'est plus proposée automatiquement, mais une recherche manuelle
    /// la propose encore. nil l'oublie.
    static var skippedVersion: String? {
        get { UserDefaults.standard.string(forKey: skippedKey) }
        set { UserDefaults.standard.set(newValue, forKey: skippedKey) }
    }

    /// À appeler une fois au lancement, avec le dépôt GitHub (« VGioM/Biblifilm »).
    static func start(repo: String) {
        self.repo = repo
        // Laisse la fenêtre s'ouvrir avant de solliciter le réseau.
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) {
            guard automatic else { return }
            if let last = UserDefaults.standard.object(forKey: lastCheckKey) as? Date,
               Date().timeIntervalSince(last) < 20 * 3600 { return }
            Task { @MainActor in await check(interactive: false) }
        }
    }

    /// « Rechercher les mises à jour… » : vérifie tout de suite et dit toujours ce qu'il en est.
    static func checkNow() {
        Task { @MainActor in await check(interactive: true) }
    }

    static let menuTitle = "Rechercher les mises à jour…"
    static let automaticTitle = "Rechercher automatiquement les mises à jour"

    /// L'entrée de menu toute faite, pour les apps AppKit.
    static func menuItem() -> NSMenuItem {
        let item = NSMenuItem(title: menuTitle, action: #selector(MenuTarget.check), keyEquivalent: "")
        item.target = MenuTarget.shared
        return item
    }

    /// L'entrée à cocher « Rechercher automatiquement les mises à jour »,
    /// pour les apps AppKit sans fenêtre de réglages.
    static func automaticMenuItem() -> NSMenuItem {
        let item = NSMenuItem(title: automaticTitle, action: #selector(MenuTarget.toggleAutomatic(_:)), keyEquivalent: "")
        item.target = MenuTarget.shared
        item.state = automatic ? .on : .off
        return item
    }

    private final class MenuTarget: NSObject {
        static let shared = MenuTarget()
        @objc func check() { SelfUpdate.checkNow() }
        @objc func toggleAutomatic(_ sender: NSMenuItem) {
            SelfUpdate.automatic.toggle()
            sender.state = SelfUpdate.automatic ? .on : .off
        }
    }

    static var appName: String {
        let info = Bundle.main.infoDictionary
        return info?["CFBundleDisplayName"] as? String ?? info?["CFBundleName"] as? String
            ?? ProcessInfo.processInfo.processName
    }

    static var currentVersion: String { version(of: Bundle.main) }

    // MARK: - Dialogue

    @MainActor
    private static func check(interactive: Bool) async {
        guard !busy, !repo.isEmpty else { return }
        busy = true
        defer { busy = false }

        let release: Release
        do {
            release = try await latest(repo: repo)
            UserDefaults.standard.set(Date(), forKey: lastCheckKey)
        } catch {
            if interactive { tell("Impossible de vérifier les mises à jour", error.localizedDescription) }
            return
        }

        guard isNewer(release.version, than: currentVersion) else {
            if interactive {
                tell("\(appName) est à jour", "Vous avez la dernière version (\(currentVersion)).")
            }
            return
        }
        if !interactive, skippedVersion == release.version { return }

        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "\(appName) \(release.version) est disponible"
        let size = release.size > 0 ? " (\(ByteCountFormatter.string(fromByteCount: Int64(release.size), countStyle: .file)))" : ""
        alert.informativeText = "Vous avez la version \(currentVersion). La mise à jour télécharge la nouvelle version\(size), remplace l'app et la relance."
        if !release.notes.isEmpty { alert.accessoryView = notesView(release.notes) }
        alert.addButton(withTitle: "Mettre à jour")
        alert.addButton(withTitle: "Plus tard")
        alert.addButton(withTitle: "Ignorer cette version")

        switch alert.runModal() {
        case .alertFirstButtonReturn:
            await installAndRelaunch(release)
        case .alertThirdButtonReturn:
            skippedVersion = release.version
        default:
            break
        }
    }

    @MainActor
    private static func installAndRelaunch(_ release: Release) async {
        let target = Bundle.main.bundleURL
        let progress = ProgressPanel(text: "Téléchargement de \(appName) \(release.version)…")
        progress.show()
        do {
            try await install(release, replacing: target)
            progress.close()
            relaunch(target)
        } catch {
            progress.close()
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "La mise à jour n'a pas pu être installée"
            alert.informativeText = (error.localizedDescription) + "\n\nVous pouvez la télécharger à la main depuis la page de la release."
            alert.addButton(withTitle: "Ouvrir la page")
            alert.addButton(withTitle: "Fermer")
            if alert.runModal() == .alertFirstButtonReturn { NSWorkspace.shared.open(release.page) }
        }
    }

    private static func notesView(_ notes: String) -> NSView {
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 380, height: 160))
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        let text = NSTextView(frame: scroll.bounds)
        text.isEditable = false
        text.string = notes
        text.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        text.textContainerInset = NSSize(width: 4, height: 6)
        text.autoresizingMask = [.width]
        scroll.documentView = text
        return scroll
    }

    @MainActor
    private static func tell(_ title: String, _ text: String) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.runModal()
    }

    @MainActor
    private final class ProgressPanel {
        private let panel: NSPanel

        init(text: String) {
            panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 340, height: 86),
                            styleMask: [.titled], backing: .buffered, defer: false)
            panel.title = SelfUpdate.appName
            let label = NSTextField(labelWithString: text)
            label.frame = NSRect(x: 20, y: 48, width: 300, height: 18)
            let bar = NSProgressIndicator(frame: NSRect(x: 20, y: 20, width: 300, height: 16))
            bar.style = .bar
            bar.isIndeterminate = true
            bar.startAnimation(nil)
            panel.contentView?.addSubview(label)
            panel.contentView?.addSubview(bar)
            panel.center()
        }

        func show() { panel.makeKeyAndOrderFront(nil) }
        func close() { panel.orderOut(nil) }
    }

    /// Relance l'app une fois ce processus terminé.
    @MainActor
    private static func relaunch(_ app: URL) {
        let pid = ProcessInfo.processInfo.processIdentifier
        let waiter = Process()
        waiter.executableURL = URL(fileURLWithPath: "/bin/sh")
        // Le chemin passe en $0 : rien n'est interprété par le shell.
        waiter.arguments = ["-c", "while kill -0 \(pid) 2>/dev/null; do sleep 0.2; done; /usr/bin/open \"$0\"", app.path]
        try? waiter.run()
        // Pas d'appel direct : on est ici dans une tâche du MainActor, donc à
        // l'intérieur de la file principale. Si l'app répond .terminateLater
        // (Biblijeu sauvegarde avant de partir), terminate attend dans une
        // boucle imbriquée qui ne vide plus la file principale : le travail
        // asynchrone qui doit lui répondre ne s'exécute jamais et l'app reste
        // ouverte indéfiniment. Passer par la boucle d'événements évite ça.
        NSApp.perform(#selector(NSApplication.terminate(_:)), with: nil, afterDelay: 0)
    }

    // MARK: - Release GitHub

    struct GitHubRelease: Decodable {
        let tag_name: String
        let html_url: String
        let body: String?
        let draft: Bool
        let prerelease: Bool
        let assets: [Asset]
        struct Asset: Decodable {
            let name: String
            let browser_download_url: String
            let size: Int
        }

        enum CodingKeys: String, CodingKey { case tag_name, html_url, body, draft, prerelease, assets }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            tag_name = try c.decode(String.self, forKey: .tag_name)
            html_url = try c.decode(String.self, forKey: .html_url)
            body = try c.decodeIfPresent(String.self, forKey: .body)
            draft = try c.decodeIfPresent(Bool.self, forKey: .draft) ?? false
            prerelease = try c.decodeIfPresent(Bool.self, forKey: .prerelease) ?? false
            assets = try c.decode([Asset].self, forKey: .assets)
        }
    }

    /// La release Mac la plus récente du dépôt (ni brouillon ni préversion).
    /// Mac et Linux partagent les releases sans partager les numéros : la
    /// dernière release peut n'avoir qu'une AppImage, on lit donc la liste
    /// et on garde la plus haute qui a un zip Mac. `feed` remplace l'adresse
    /// de l'API (un tableau JSON), pour les tests.
    static func latest(repo: String, feed: URL? = nil) async throws -> Release {
        let url = feed ?? URL(string: "https://api.github.com/repos/\(repo)/releases?per_page=30")!
        var request = URLRequest(url: url, timeoutInterval: 15)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("\(appName.replacingOccurrences(of: " ", with: ""))-updater", forHTTPHeaderField: "User-Agent")
        let data: Data, response: URLResponse
        do { (data, response) = try await URLSession.shared.data(for: request) }
        catch { throw Failure.network(error.localizedDescription) }
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            throw Failure.network("code \(http.statusCode)")
        }
        let releases: [GitHubRelease]
        do { releases = try JSONDecoder().decode([GitHubRelease].self, from: data) }
        catch { throw Failure.network("réponse illisible") }

        guard let (release, asset) = pick(releases),
              let download = URL(string: asset.browser_download_url) else { throw Failure.noZip }
        return Release(version: clean(release.tag_name),
                       notes: readableNotes(release.body ?? ""),
                       page: URL(string: release.html_url) ?? URL(string: "https://github.com/\(repo)/releases")!,
                       download: download,
                       size: asset.size)
    }

    /// Parmi les releases, la plus haute (par numéro de tag) qui n'est ni
    /// brouillon ni préversion et qui contient un zip Mac, avec ce zip.
    static func pick(_ releases: [GitHubRelease]) -> (GitHubRelease, GitHubRelease.Asset)? {
        var best: (GitHubRelease, GitHubRelease.Asset)?
        for release in releases where !release.draft && !release.prerelease {
            guard let name = macZip(among: release.assets.map(\.name)),
                  let asset = release.assets.first(where: { $0.name == name }) else { continue }
            if let current = best, !isNewer(clean(release.tag_name), than: clean(current.0.tag_name)) { continue }
            best = (release, asset)
        }
        return best
    }

    /// Le zip Mac de la release.
    ///
    /// Une même release porte les fichiers de toutes les plateformes, nommés
    /// `<App>-<version>-<plateforme>-<architecture>.<ext>` :
    /// `-macOS-arm64.zip`, `-linux-x86_64.tar.gz`, `-windows-x64.zip`…
    /// Seul un zip marqué macOS est pris ; un zip sans aucune plateforme dans
    /// son nom (anciennes releases) aussi, mais jamais celui d'une autre
    /// plateforme. Une release qui n'a pas encore sa version Mac n'est donc
    /// pas proposée.
    static func macZip(among names: [String]) -> String? {
        let zips = names.filter { $0.lowercased().hasSuffix(".zip") }
        if let mac = zips.first(where: { $0.lowercased().contains("macos") }) { return mac }
        let elsewhere = ["windows", "win32", "win64", "linux", "appimage"]
        return zips.first { name in !elsewhere.contains { name.lowercased().contains($0) } }
    }

    /// Les notes telles qu'on les lit, sans le Markdown ni la partie « Installation ».
    static func readableNotes(_ body: String) -> String {
        var text = body.replacingOccurrences(of: "\r\n", with: "\n")
        if let cut = text.range(of: "## Installation") { text = String(text[..<cut.lowerBound]) }
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false).map { line -> String in
            var l = String(line)
            while l.hasPrefix("#") { l.removeFirst() }
            if l.hasPrefix("- ") { l = "•" + l.dropFirst(1) }
            return l.replacingOccurrences(of: "**", with: "").replacingOccurrences(of: "`", with: "")
                .trimmingCharacters(in: .whitespaces)
        }
        return lines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - Versions

    static func clean(_ tag: String) -> String {
        var t = tag.trimmingCharacters(in: .whitespaces)
        if t.lowercased().hasPrefix("v") { t.removeFirst() }
        return t
    }

    static func isNewer(_ a: String, than b: String) -> Bool {
        let x = parts(a), y = parts(b)
        for i in 0..<max(x.count, y.count) {
            let p = i < x.count ? x[i] : 0, q = i < y.count ? y[i] : 0
            if p != q { return p > q }
        }
        return false
    }

    private static func parts(_ version: String) -> [Int] {
        clean(version).split(separator: ".").map { Int($0.prefix(while: \.isNumber)) ?? 0 }
    }

    private static func version(of bundle: Bundle) -> String {
        bundle.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0"
    }

    // MARK: - Installation

    /// Télécharge, vérifie et met la nouvelle version à la place de `target`.
    static func install(_ release: Release, replacing target: URL) async throws {
        if target.path.contains("/AppTranslocation/") { throw Failure.translocated }
        let fm = FileManager.default
        guard fm.isWritableFile(atPath: target.deletingLastPathComponent().path),
              fm.isWritableFile(atPath: target.path) else { throw Failure.notWritable }

        // Sur le même volume que l'app quand c'est possible : le remplacement
        // final est alors un simple échange.
        let staging = (try? fm.url(for: .itemReplacementDirectory, in: .userDomainMask,
                                   appropriateFor: target, create: true))
            ?? fm.temporaryDirectory.appendingPathComponent("SelfUpdate-\(UUID().uuidString)")
        try fm.createDirectory(at: staging, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: staging) }

        let downloaded: URL, response: URLResponse
        do { (downloaded, response) = try await URLSession.shared.download(from: release.download) }
        catch { throw Failure.network(error.localizedDescription) }
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            throw Failure.network("code \(http.statusCode)")
        }
        let zip = staging.appendingPathComponent("update.zip")
        try fm.moveItem(at: downloaded, to: zip)

        let unpacked = staging.appendingPathComponent("unpacked")
        guard run("/usr/bin/ditto", ["-x", "-k", zip.path, unpacked.path]) else { throw Failure.unzip }
        guard let app = try fm.contentsOfDirectory(at: unpacked, includingPropertiesForKeys: nil)
            .first(where: { $0.pathExtension == "app" }) else { throw Failure.noApp }

        try verify(app, against: target)
        _ = run("/usr/bin/xattr", ["-dr", "com.apple.quarantine", app.path])
        _ = try fm.replaceItemAt(target, withItemAt: app)
    }

    /// Refuse tout ce qui ne serait pas une version plus récente de la même
    /// app, signée par le même certificat.
    static func verify(_ app: URL, against current: URL) throws {
        guard let new = Bundle(path: app.path), let old = Bundle(path: current.path) else { throw Failure.noApp }
        guard new.bundleIdentifier != nil, new.bundleIdentifier == old.bundleIdentifier else { throw Failure.wrongApp }
        guard isNewer(version(of: new), than: version(of: old)) else { throw Failure.notNewer }

        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(app as CFURL, [], &code) == errSecSuccess, let code else {
            throw Failure.signature("illisible")
        }
        let flags = SecCSFlags(rawValue: kSecCSCheckAllArchitectures | kSecCSStrictValidate | kSecCSCheckNestedCode)
        let status = SecStaticCodeCheckValidity(code, flags, certificateRequirement(of: current))
        guard status == errSecSuccess else {
            throw Failure.signature(SecCopyErrorMessageString(status, nil) as String? ?? "erreur \(status)")
        }
    }

    /// L'exigence de signature de l'app en place, si elle est signée avec un
    /// certificat. Une signature ad hoc n'en a pas d'utilisable : elle ne
    /// désigne que ce binaire précis.
    private static func certificateRequirement(of app: URL) -> SecRequirement? {
        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(app as CFURL, [], &code) == errSecSuccess, let code else { return nil }
        var info: CFDictionary?
        guard SecCodeCopySigningInformation(code, SecCSFlags(rawValue: kSecCSSigningInformation), &info) == errSecSuccess,
              let certificates = (info as? [String: Any])?[kSecCodeInfoCertificates as String] as? [Any],
              !certificates.isEmpty else { return nil }
        var requirement: SecRequirement?
        guard SecCodeCopyDesignatedRequirement(code, [], &requirement) == errSecSuccess else { return nil }
        return requirement
    }

    @discardableResult
    private static func run(_ tool: String, _ arguments: [String]) -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: tool)
        process.arguments = arguments
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return false }
        process.waitUntilExit()
        return process.terminationStatus == 0
    }
}
