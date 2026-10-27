"""Isolated Word conversion helper; invoked only by docx_tailor's subprocess."""
from pathlib import Path
import sys


def convert(source, target):
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = document = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible, word.DisplayAlerts = False, 0
        word.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
        word.Options.UpdateLinksAtOpen = False
        document = word.Documents.Open(str(Path(source).resolve()), ReadOnly=True,
                                       AddToRecentFiles=False, Visible=False)
        document.ExportAsFixedFormat(str(Path(target).resolve()), 17)  # wdExportFormatPDF
    finally:
        try:
            if document is not None:
                document.Close(False)
        finally:
            document = None
            try:
                if word is not None:
                    word.Quit()
            finally:
                word = None
                pythoncom.CoUninitialize()


def main():
    try:
        convert(*sys.argv[1:3])
    except Exception as exc:
        # Do not expose document text, file paths, or COM exception details.
        print("Word conversion failed: " + type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
