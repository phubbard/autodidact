#!/usr/bin/env python3
"""Build the "Send to Autodidact" share-sheet Shortcut as a file, so nobody has to click it together.

    python3 docs/make_shortcut.py                      # writes "Send to Autodidact (unsigned).shortcut"
    shortcuts sign --mode anyone --input "Send to Autodidact (unsigned).shortcut" --output "Send to Autodidact.shortcut"
    open "Send to Autodidact.shortcut"                 # Shortcuts offers "Add Shortcut"

The bearer token is NOT embedded: the Authorization header reads "Bearer PASTE-TOKEN-HERE". After
importing, expand "Get contents of" and paste the token from /srv/autodidact/env over the placeholder.
The imported name comes from the signed file's name. See docs/SHORTCUT.md for what each step does.

Serialization notes, learned the hard way: "Get URLs from" takes a *text* parameter, so Shortcut Input
must be embedded as a WFTextTokenString attachment; as a bare WFTextTokenAttachment the field silently
stays empty and the server receives "url": "".
"""
import argparse
import plistlib
import uuid

SERVER = "https://autodidact.phfactor.net/ingest-url"


def build(server: str = SERVER) -> dict:
    U = lambda: str(uuid.uuid4()).upper()  # noqa: E731
    u_urls, u_first, u_dev, u_post, u_msg = U(), U(), U(), U(), U()

    def text(s):
        return {"Value": {"string": s, "attachmentsByRange": {}}, "WFSerializationType": "WFTextTokenString"}

    def var(ref):  # a variable as the whole content of a text field
        return {"Value": {"string": "￼", "attachmentsByRange": {"{0, 1}": ref}},
                "WFSerializationType": "WFTextTokenString"}

    def out(uid, name):
        return {"Type": "ActionOutput", "OutputUUID": uid, "OutputName": name}

    def attach(ref):
        return {"Value": ref, "WFSerializationType": "WFTextTokenAttachment"}

    def fields(items):
        return {"Value": {"WFDictionaryFieldValueItems": [{"WFItemType": 0, "WFKey": text(k), "WFValue": v}
                                                          for k, v in items]},
                "WFSerializationType": "WFDictionaryFieldValue"}

    actions = [
        ("is.workflow.actions.detect.link", {"UUID": u_urls, "WFInput": var({"Type": "ExtensionInput"})}),
        ("is.workflow.actions.getitemfromlist",
         {"UUID": u_first, "WFItemSpecifier": "First Item", "WFInput": attach(out(u_urls, "URLs"))}),
        ("is.workflow.actions.getdevicedetails", {"UUID": u_dev, "WFDeviceDetail": "Device Name"}),
        ("is.workflow.actions.downloadurl", {
            "UUID": u_post, "WFURL": server, "WFHTTPMethod": "POST", "ShowHeaders": True,
            "WFHTTPHeaders": fields([("Authorization", text("Bearer PASTE-TOKEN-HERE"))]),
            "WFHTTPBodyType": "JSON",
            "WFJSONValues": fields([("url", var(out(u_first, "Item from List"))),
                                    ("client", var(out(u_dev, "Device Details")))]),
        }),
        # The server always answers with a ready-to-show "message" ("Saved: …", "Not saved: …").
        ("is.workflow.actions.getvalueforkey", {"UUID": u_msg, "WFGetDictionaryValueType": "Value",
                                                "WFDictionaryKey": "message",
                                                "WFInput": attach(out(u_post, "Contents of URL"))}),
        ("is.workflow.actions.notification", {"WFNotificationActionTitle": "Autodidact",
                                              "WFNotificationActionBody": var(out(u_msg, "Dictionary Value")),
                                              "WFNotificationActionSound": False}),
    ]
    return {
        "WFWorkflowClientVersion": "2607.0.2", "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 463140863, "WFWorkflowIconGlyphNumber": 59446},
        "WFWorkflowImportQuestions": [], "WFQuickActionSurfaces": [],
        "WFWorkflowTypes": ["ActionExtension"],  # appears in the share sheet
        "WFWorkflowInputContentItemClasses": ["WFURLContentItem", "WFSafariWebPageContentItem",
                                              "WFArticleContentItem", "WFRichTextContentItem"],
        "WFWorkflowOutputContentItemClasses": [], "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowNoInputBehavior": {"Name": "WFWorkflowNoInputBehaviorGetClipboard", "Parameters": {}},
        "WFWorkflowActions": [{"WFWorkflowActionIdentifier": i, "WFWorkflowActionParameters": p} for i, p in actions],
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=SERVER, help="ingest-url endpoint")
    ap.add_argument("--output", default="Send to Autodidact (unsigned).shortcut")
    args = ap.parse_args()
    with open(args.output, "wb") as f:
        plistlib.dump(build(args.server), f, fmt=plistlib.FMT_BINARY)
    print(f"wrote {args.output}; now: shortcuts sign --mode anyone --input '{args.output}' "
          f"--output 'Send to Autodidact.shortcut' && open 'Send to Autodidact.shortcut'")
