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
!include "LogicLib.nsh"
!define MUI_ICON   "roastmesh.ico"
!define MUI_UNICON "roastmesh.ico"

!define MUI_FINISHPAGE_RUN "$INSTDIR\roastmesh-gui.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Open roastmesh"

; The installer now asks for the firewall rule itself (see SecMain), so this
; page no longer depends on the user catching a prompt at the right moment.
; It still says what to do if that did not take, because a blocked roastmesh
; looks exactly like a working one that simply finds nobody.
!define MUI_FINISHPAGE_TITLE "roastmesh is installed"
!define MUI_FINISHPAGE_TEXT "roastmesh asked Windows Firewall to allow incoming connections on private and domain networks, so other machines can reach this one.$\r$\n$\r$\nIf you declined that prompt, or if Windows later asks again, allow it for PRIVATE NETWORKS. Without it roastmesh can still find other machines, but they cannot find this one -- it looks like it is working and quietly receives nothing.$\r$\n$\r$\nYou can change this at any time under Windows Security > Firewall & network protection > Allow an app through firewall."

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

  Call AddFirewallRule

  WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd

; Only roastmesh.exe needs a rule. It is the process that serves: the GUI
; shells out to it for everything (gui/runner.py's roastmesh_argv), and binds
; nothing itself except a loopback port for single-instance detection, which
; no firewall ever blocks.
;
; private,domain and deliberately not public: a home or studio network is
; where roastmesh is meant to be reachable, and a cafe network is exactly
; where accepting unsolicited inbound connections is not a favour.
!define FW_RULE 'name="roastmesh" dir=in action=allow program="$INSTDIR\roastmesh.exe" enable=yes profile=private,domain'

Function AddFirewallRule
  DetailPrint "Allowing roastmesh through Windows Firewall..."
  ; Direct first: succeeds when the installer already happens to be elevated,
  ; which is the case in CI and for anyone who ran it as administrator.
  nsExec::ExecToLog 'netsh.exe advfirewall firewall add rule ${FW_RULE}'
  Pop $0
  ${If} $0 != 0
    ; A per-user install has no UAC prompt by design, so this is the one place
    ; that asks for one -- and only interactively. Raising a UAC dialog during
    ; a /S silent install would hang whatever automation invoked it.
    ${IfNot} ${Silent}
      ExecShellWait "runas" "netsh.exe" 'advfirewall firewall add rule ${FW_RULE}' SW_HIDE
    ${EndIf}
  ${EndIf}
  ; Never fatal. Declining the prompt leaves roastmesh able to reach out but
  ; not to be reached, which the finish page explains -- it is not a reason to
  ; fail an install that has otherwise completely succeeded.
  ClearErrors
FunctionEnd

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

  ; Leaving a firewall exception behind for a program that no longer exists is
  ; the kind of quiet residue an uninstall exists to prevent.
  nsExec::ExecToLog 'netsh.exe advfirewall firewall delete rule name="roastmesh"'
  Pop $0
  ${If} $0 != 0
    ${IfNot} ${Silent}
      ExecShellWait "runas" "netsh.exe" 'advfirewall firewall delete rule name="roastmesh"' SW_HIDE
    ${EndIf}
  ${EndIf}
  ClearErrors

  DeleteRegKey HKCU "${REGKEY}"
  DeleteRegKey HKCU "Software\roastmesh"

  ; Deliberately NOT removed: %USERPROFILE%\.local\share\roastmesh and
  ; .config\roastmesh. Those hold the user's signing identity, their feed, and
  ; their search index -- an identity cannot be regenerated, and deleting a
  ; feed would silently orphan everything already replicated to peers. An
  ; uninstall means "remove the program", not "destroy my roasts".
SectionEnd
