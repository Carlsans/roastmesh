; NSIS installer for roastnet on Windows.
;
; Chosen over Inno Setup/WiX because NSIS is preinstalled on GitHub's Windows
; runners, so building the installer adds no setup step to CI.
;
; Per-user by design: everything goes under %LOCALAPPDATA%, so there is no UAC
; prompt and no admin account needed. roastnet writes only to the user's own
; profile at runtime anyway, so a machine-wide install would buy nothing.
;
; Build:  makensis /DVERSION=0.3.17 packaging/roastnet.nsi
; (run from the repo root, with dist\roastnet.exe and dist\roastnet-gui.exe present)

!ifndef VERSION
  !define VERSION "0.0.0"
!endif

!define APPNAME    "roastnet"
!define PUBLISHER  "roastnet"
!define REGKEY     "Software\Microsoft\Windows\CurrentVersion\Uninstall\roastnet"

Name            "${APPNAME} ${VERSION}"
; Relative to THIS script's directory (packaging\), not the working directory
; makensis was invoked from -- so `..\dist` is the repo's dist folder.
OutFile         "..\dist\roastnet-setup-x86_64.exe"
Unicode         True
RequestExecutionLevel user          ; per-user: no UAC prompt
InstallDir      "$LOCALAPPDATA\Programs\roastnet"
InstallDirRegKey HKCU "Software\roastnet" "InstallDir"
SetCompressor /SOLID lzma
BrandingText    "${APPNAME} ${VERSION}"

!include "MUI2.nsh"
!define MUI_ICON   "roastnet.ico"
!define MUI_UNICON "roastnet.ico"

!define MUI_FINISHPAGE_RUN "$INSTDIR\roastnet-gui.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Open roastnet"

!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "roastnet" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"

  ; Both binaries must land in the same directory: roastnet-gui.exe resolves
  ; the CLI as its own sibling (gui/runner.py's roastnet_argv), so shipping
  ; the GUI alone would leave every button in the app unable to do anything.
  File "..\dist\roastnet.exe"
  File "..\dist\roastnet-gui.exe"
  File "roastnet.ico"

  CreateShortCut "$SMPROGRAMS\roastnet.lnk" "$INSTDIR\roastnet-gui.exe" "" "$INSTDIR\roastnet.ico"

  WriteRegStr HKCU "Software\roastnet" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${REGKEY}" "DisplayName"     "${APPNAME}"
  WriteRegStr HKCU "${REGKEY}" "DisplayVersion"  "${VERSION}"
  WriteRegStr HKCU "${REGKEY}" "Publisher"       "${PUBLISHER}"
  WriteRegStr HKCU "${REGKEY}" "DisplayIcon"     "$INSTDIR\roastnet.ico"
  WriteRegStr HKCU "${REGKEY}" "UninstallString" "$INSTDIR\uninstall.exe"
  WriteRegDWORD HKCU "${REGKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${REGKEY}" "NoRepair" 1

  WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd

Section "Uninstall"
  Delete "$INSTDIR\roastnet.exe"
  Delete "$INSTDIR\roastnet-gui.exe"
  Delete "$INSTDIR\roastnet.ico"
  Delete "$INSTDIR\uninstall.exe"
  RMDir  "$INSTDIR"
  Delete "$SMPROGRAMS\roastnet.lnk"

  DeleteRegKey HKCU "${REGKEY}"
  DeleteRegKey HKCU "Software\roastnet"

  ; Deliberately NOT removed: %USERPROFILE%\.local\share\roastnet and
  ; .config\roastnet. Those hold the user's signing identity, their feed, and
  ; their search index -- an identity cannot be regenerated, and deleting a
  ; feed would silently orphan everything already replicated to peers. An
  ; uninstall means "remove the program", not "destroy my roasts".
SectionEnd
