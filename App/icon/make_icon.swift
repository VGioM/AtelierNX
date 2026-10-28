// Dessine l'icône d'Atelier NX (1024 px) : swift App/icon/make_icon.swift <sortie.png>
import AppKit

let size: CGFloat = 1024
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()
let ctx = NSGraphicsContext.current!.cgContext

// Fond : carré arrondi façon macOS, anthracite → noir
let inset: CGFloat = 100, r: CGFloat = 185
let bg = NSBezierPath(roundedRect: NSRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset), xRadius: r, yRadius: r)
ctx.saveGState()
let shadow = NSShadow(); shadow.shadowBlurRadius = 28; shadow.shadowOffset = NSSize(width: 0, height: -12); shadow.shadowColor = NSColor.black.withAlphaComponent(0.45); shadow.set()
NSColor.black.setFill(); bg.fill()
ctx.restoreGState()
bg.addClip()
NSGradient(colors: [NSColor(calibratedRed: 0.20, green: 0.20, blue: 0.24, alpha: 1), NSColor(calibratedRed: 0.07, green: 0.07, blue: 0.09, alpha: 1)])!.draw(in: bg, angle: -90)

// Engrenage rouge (l'atelier)
let c = CGPoint(x: size / 2, y: size / 2 + 10)
let teeth = 10, rOut: CGFloat = 300, rIn: CGFloat = 255
let gear = NSBezierPath()
for i in 0..<(teeth * 4) {
    let a = CGFloat(i) / CGFloat(teeth * 4) * 2 * .pi - .pi / 2
    let rad = (i % 4 == 0 || i % 4 == 1) ? rOut : rIn
    let p = CGPoint(x: c.x + cos(a) * rad, y: c.y + sin(a) * rad)
    i == 0 ? gear.move(to: p) : gear.line(to: p)
}
gear.close()
gear.appendOval(in: NSRect(x: c.x - 205, y: c.y - 205, width: 410, height: 410))
gear.windingRule = .evenOdd
NSGradient(colors: [NSColor(calibratedRed: 1.0, green: 0.36, blue: 0.30, alpha: 1), NSColor(calibratedRed: 0.86, green: 0.05, blue: 0.20, alpha: 1)])!.draw(in: gear, angle: -90)

// « NX » au centre
let para = NSMutableParagraphStyle(); para.alignment = .center
let font = NSFont.systemFont(ofSize: 250, weight: .black)
let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.white, .paragraphStyle: para, .kern: -8]
let text = NSAttributedString(string: "NX", attributes: attrs)
let h = text.size().height
text.draw(in: NSRect(x: 0, y: c.y - h / 2 + 8, width: size, height: h))
img.unlockFocus()

let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
