; NSIS installer for roastmesh on Windows.
;
; Chosen over Inno Setup/WiX because NSIS is preinstalled on GitHub's Windows
; runners, so building the installer adds no setup step to CI.
;
; Per-user by design: everything goes under %LOCALAPPDATA%, so there is no UAC
; prompt and no admin account needed. roastmesh writes only to the user's own
; profile at runtime anyway, so a machine-wide install would buy nothing.
;
; Build:  makensis /DVERSION=0.3.17 packaging/roastmesh.nsi
; (run from the repo root, with dist\roastmesh.exe and dist\roastmesh-gui.exe present)

!ifndef VERSION
  !define VERSION "0.0.0"
!endif

!define APPNAME    "roastmesh"
!define PUBLISHER  "roastmesh"
!define REGKEY     "Software\Microsoft\Windows\CurrentVersion\Uninstall\roastmesh"

Name            "${APPNAME} ${VERSION}"
; Relative to THIS script's directory (packaging\), not the working directory
; makensis was invoked from -- so `..\dist` is the repo's dist folder.
OutFile         "..\dist\roastmesh-setup-x86_64.exe"
Unicode         True
RequestExecutionLevel user          ; per-user: no UAC prompt
InstallDir      "$LOCALAPPDATA\Programs\roastmesh"
InstallDirRegKey HKCU "Software\roastmesh" "InstallDir"
SetCompressor /SOLID lzma
BrandingText    "${APPNAME} ${VERSION}"

!include "MUI2.nsh"
!define MUI_ICON   "roastmesh.ico"
!define MUI_UNICON "roastmesh.ico"

!define MUI_FINISHPAGE_RUN "$INSTDIR\roastmesh-gui.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Open roastmesh"

; Shown at exactly the moment it matters: Windows raises its firewall prompt
; the first time roastmesh opens a listening socket, which is seconds after
; this page. "Private networks" is unticked by default in some Windows
; configurations, and without it the LAN beacon is blocked -- roastmesh then
; finds nobody on the home network and looks broken, with nothing to indicate
; a firewall is the cause.
!define MUI_FINISHPAGE_TITLE "roastmesh is installed"
!define MUI_FINISHPAGE_TEXT "One thing before you start.$\r$\n$\r$\nWhen Windows asks whether to allow roastmesh on a network, tick PRIVATE NETWORKS. Windows Firewall otherwise blocks the beacon roastmesh uses to find other machines on your home or studio network, and it will quietly find nobody there.$\r$\n$\r$\nIf you miss the prompt, you can fix it later under Windows Security > Firewall & network protection > Allow an app through firewall.$\r$\n$\r$\nSharing over the internet does not depend on this."

!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "roastmesh" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"

  ; The Windows build is onedir (see roastmesh.spec): dist\roastmesh\ holds
  ; both .exe files plus the shared _internal\ runtime they both load. The
  ; whole tree has to be installed -- an .exe without its _internal\ sibling
  ; cannot start at all.
  ;
  ; Both binaries must also land in the same directory: roastmesh-gui.exe
  ; resolves the CLI as its own sibling (gui/runner.py's roastmesh_argv), so
  ; shipping the GUI alone would leave every button in the app unable to do
  ; anything. COLLECT already places them together; /r preserves that.
  File /r "..\dist\roastmesh\*.*"
  File "roastmesh.ico"

  CreateShortCut "$SMPROGRAMS\roastmesh.lnk" "$INSTDIR\roastmesh-gui.exe" "" "$INSTDIR\roastmesh.ico"

  WriteRegStr HKCU "Software\roastmesh" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${REGKEY}" "DisplayName"     "${APPNAME}"
  WriteRegStr HKCU "${REGKEY}" "DisplayVersion"  "${VERSION}"
  WriteRegStr HKCU "${REGKEY}" "Publisher"       "${PUBLISHER}"
  WriteRegStr HKCU "${REGKEY}" "DisplayIcon"     "$INSTDIR\roastmesh.ico"
  WriteRegStr HKCU "${REGKEY}" "UninstallString" "$INSTDIR\uninstall.exe"
  WriteRegDWORD HKCU "${REGKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${REGKEY}" "NoRepair" 1

  WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd

Section "Uninstall"
  Delete "$INSTDIR\roastmesh.exe"
  Delete "$INSTDIR\roastmesh-gui.exe"
  Delete "$INSTDIR\roastmesh.ico"
  Delete "$INSTDIR\uninstall.exe"
  ; onedir ships a _internal\ tree beside the executables; a plain RMDir only
  ; removes an already-empty directory, so without this the uninstaller would
  ; leave ~50MB behind and silently fail to remove $INSTDIR at all.
  RMDir /r "$INSTDIR\_internal"
  RMDir  "$INSTDIR"
  Delete "$SMPROGRAMS\roastmesh.lnk"

  DeleteRegKey HKCU "${REGKEY}"
  DeleteRegKey HKCU "Software\roastmesh"

  ; Deliberately NOT removed: %USERPROFILE%\.local\share\roastmesh and
  ; .config\roastmesh. Those hold the user's signing identity, their feed, and
  ; their search index -- an identity cannot be regenerated, and deleting a
  ; feed would silently orphan everything already replicated to peers. An
  ; uninstall means "remove the program", not "destroy my roasts".
SectionEnd
