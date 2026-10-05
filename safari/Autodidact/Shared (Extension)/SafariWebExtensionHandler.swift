//
//  SafariWebExtensionHandler.swift
//  Shared (Extension)
//
//  Created by Paul Hubbard on 10/1/26.
//
//  Answers browser.runtime.sendNativeMessage from background.js. Its one job is
//  keeping a copy of the extension's settings (server URL, token, device name,
//  dwell threshold, blocklist) in this app extension's UserDefaults. Those
//  survive app updates; Safari's storage.local for the extension did not when
//  the iOS app was reinstalled on 2026-10-05.
//

import SafariServices
import os.log

class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {

    private static let settingsKey = "autodidact.settings"

    func beginRequest(with context: NSExtensionContext) {
        let request = context.inputItems.first as? NSExtensionItem

        let message: Any?
        if #available(iOS 15.0, macOS 11.0, *) {
            message = request?.userInfo?[SFExtensionMessageKey]
        } else {
            message = request?.userInfo?["message"]
        }

        let reply = handle(message as? [String: Any])

        let response = NSExtensionItem()
        if #available(iOS 15.0, macOS 11.0, *) {
            response.userInfo = [ SFExtensionMessageKey: reply ]
        } else {
            response.userInfo = [ "message": reply ]
        }
        context.completeRequest(returningItems: [ response ], completionHandler: nil)
    }

    private func handle(_ message: [String: Any]?) -> [String: Any] {
        let type = message?["type"] as? String ?? ""
        // Log the message type only; the settings include the bearer token.
        os_log(.default, "autodidact native message: %{public}@", type)

        let defaults = UserDefaults.standard
        switch type {
        case "saveSettings":
            guard let settings = message?["settings"] as? [String: Any],
                  JSONSerialization.isValidJSONObject(settings),
                  let data = try? JSONSerialization.data(withJSONObject: settings) else {
                return ["ok": false, "error": "settings missing or not JSON"]
            }
            defaults.set(data, forKey: Self.settingsKey)
            return ["ok": true]

        case "loadSettings":
            guard let data = defaults.data(forKey: Self.settingsKey),
                  let settings = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                return ["ok": true, "settings": NSNull()]
            }
            return ["ok": true, "settings": settings]

        default:
            return ["ok": false, "error": "unknown message type"]
        }
    }

}
