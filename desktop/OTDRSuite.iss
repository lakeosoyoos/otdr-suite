; Inno Setup script for OTDR Suite — per-user Windows installer.
; ======================================================================
; Compiled in CI (iscc) AFTER the PyInstaller one-folder build + boot
; self-test, from the desktop/ working dir.  Why an installer (vs the raw
; zip): it installs to ONE fixed per-user location and REMOVES the previous
; version on upgrade, so old versions can't pile up or be run by mistake.
;
;  * PrivilegesRequired=lowest  -> installs per-user (no admin prompt); on a
;    "lowest" run {autopf} resolves to %LOCALAPPDATA%\Programs.
;  * AppId is a FIXED GUID -> Inno recognises an existing install as the same
;    app and upgrades it in place (and the ARP/uninstall entry is reused).
;  * [InstallDelete] wipes the install dir before copying the new build so a
;    file removed between versions doesn't linger (a plain overwrite would
;    leave orphans).
;
; AppVersion is passed by CI:  iscc /DAppVersion=1.0.<run_number> OTDRSuite.iss
; Falls back to a dev value for local compiles.

; EDITION: this branch builds "OTDR Suite App", which installs BESIDE the
; regular OTDR Suite.  Its own AppId (so neither installer upgrades or removes
; the other), its own folder, Start-menu entry and uninstall entry.  The
; regular edition is AppName "OTDR Suite", AppId B7E5B0E2-..., OTDRSuite-Setup.
#define AppName     "OTDR Suite App"
#define AppExeName  "OTDRSuite.exe"
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
AppId={{90A888DE-8422-4311-885D-74C12BE36AC2}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Lake Osoyoos
AppPublisherURL=https://github.com/lakeosoyoos/otdr-suite
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
OutputDir=dist
OutputBaseFilename=OTDRSuiteApp-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}
SetupLogging=yes
; Tell Explorer to refresh file-type icons/handlers after [Registry] below.
ChangesAssociations=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

; Clean upgrade — clear the previous install's files before copying the new
; build so nothing orphaned remains.  {app} is our own install dir.
[InstallDelete]
Type: filesandordirs; Name: "{app}\*"

[Files]
; The PyInstaller one-folder output (built into desktop/dist/OTDRSuite by CI).
Source: "dist\OTDRSuite\*"; DestDir: "{app}"; \
  Flags: recursesubdirs createallsubdirs ignoreversion

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

; ── File associations (per-user, HKA = HKCU under PrivilegesRequired=lowest)
; Double-clicking a .zfc (phone Field Capture package), .zdb (shared project)
; or legacy .otdrproject runs OTDRSuite.exe "%1"; the launcher hands the path
; to the hub.  Standard pattern: our own ProgID + OpenWithProgids entry, and
; the extension's default value.  Everything is removed on uninstall
; (uninsdeletekey on our ProgIDs, uninsdeletevalue on the extension values).
[Registry]
; OTDR Suite App owns these file types (Projects is an App feature; the
; regular OTDR Suite never registers them).  App-only ProgIDs (OTDRSuiteApp.*)
; so installing, upgrading or uninstalling either edition cannot touch the
; other's registry keys.
Root: HKA; Subkey: "Software\Classes\.zfc"; ValueType: string; ValueName: ""; ValueData: "OTDRSuiteApp.zfc"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.zfc\OpenWithProgids"; ValueType: string; ValueName: "OTDRSuiteApp.zfc"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\OTDRSuiteApp.zfc"; ValueType: string; ValueName: ""; ValueData: "{#AppName} Field Capture"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\OTDRSuiteApp.zfc\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"
Root: HKA; Subkey: "Software\Classes\OTDRSuiteApp.zfc\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""

Root: HKA; Subkey: "Software\Classes\.zdb"; ValueType: string; ValueName: ""; ValueData: "OTDRSuiteApp.zdb"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.zdb\OpenWithProgids"; ValueType: string; ValueName: "OTDRSuiteApp.zdb"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\OTDRSuiteApp.zdb"; ValueType: string; ValueName: ""; ValueData: "{#AppName} Project"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\OTDRSuiteApp.zdb\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"
Root: HKA; Subkey: "Software\Classes\OTDRSuiteApp.zdb\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""

Root: HKA; Subkey: "Software\Classes\.otdrproject"; ValueType: string; ValueName: ""; ValueData: "OTDRSuiteApp.zdb"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.otdrproject\OpenWithProgids"; ValueType: string; ValueName: "OTDRSuiteApp.zdb"; ValueData: ""; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; \
  Flags: nowait postinstall skipifsilent
