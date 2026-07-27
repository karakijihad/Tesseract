; The app's data (memory, vault, .env) lives at $LOCALAPPDATA\com.tesseract.mirror,
; a sibling of the install dir, not inside it — the default NSIS uninstall never
; touches it. Ask explicitly instead of guessing; /SD IDNO makes "keep the data"
; the answer for any silent/unattended uninstall, so data is never deleted without
; an explicit interactive Yes.
!macro NSIS_HOOK_POSTUNINSTALL
  MessageBox MB_YESNO|MB_ICONQUESTION "Also delete TESSERACT's saved data (memory, vault, settings, .env) at$\r$\n$LOCALAPPDATA\com.tesseract.mirror ?$\r$\n$\r$\nChoose No to keep it for a future reinstall." /SD IDNO IDYES delete_data
  Goto keep_data
  delete_data:
    RMDir /r "$LOCALAPPDATA\com.tesseract.mirror"
  keep_data:
!macroend
