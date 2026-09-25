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

#define AppName     "OTDR Suite"
#define AppExeName  "OTDRSuite.exe"
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
AppId={{B7E5B0E2-3C4A-4F1D-9A6E-7C2D9F0A1B23}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Lake Osoyoos
AppPublisherURL=https://github.com/lakeosoyoos/otdr-suite
DefaultDirName={autopf}\OTDR Suite
DefaultGroupName=OTDR Suite
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
OutputDir=dist
OutputBaseFilename=OTDRSuite-Setup
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
Name: "{group}\OTDR Suite"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\OTDR Suite"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

; ── File associations (per-user, HKA = HKCU under PrivilegesRequired=lowest)
; Double-clicking a .zfc (phone Field Capture package), .zdb (shared project)
; or legacy .otdrproject runs OTDRSuite.exe "%1"; the launcher hands the path
; to the hub.  Standard pattern: our own ProgID + OpenWithProgids entry, and
; the extension's default value.  Everything is removed on uninstall
; (uninsdeletekey on our ProgIDs, uninsdeletevalue on the extension values).
[Registry]
Root: HKA; Subkey: "Software\Classes\.zfc"; ValueType: string; ValueName: ""; ValueData: "OTDRSuite.zfc"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.zfc\OpenWithProgids"; ValueType: string; ValueName: "OTDRSuite.zfc"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\OTDRSuite.zfc"; ValueType: string; ValueName: ""; ValueData: "OTDR Suite Field Capture"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\OTDRSuite.zfc\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"
Root: HKA; Subkey: "Software\Classes\OTDRSuite.zfc\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""

Root: HKA; Subkey: "Software\Classes\.zdb"; ValueType: string; ValueName: ""; ValueData: "OTDRSuite.zdb"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.zdb\OpenWithProgids"; ValueType: string; ValueName: "OTDRSuite.zdb"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\OTDRSuite.zdb"; ValueType: string; ValueName: ""; ValueData: "OTDR Suite Project"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\OTDRSuite.zdb\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"
Root: HKA; Subkey: "Software\Classes\OTDRSuite.zdb\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""

Root: HKA; Subkey: "Software\Classes\.otdrproject"; ValueType: string; ValueName: ""; ValueData: "OTDRSuite.zdb"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.otdrproject\OpenWithProgids"; ValueType: string; ValueName: "OTDRSuite.zdb"; ValueData: ""; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch OTDR Suite"; \
  Flags: nowait postinstall skipifsilent
