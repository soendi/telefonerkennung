#ifndef MyAppVersion
#define MyAppVersion "3.2.20"
#endif

[Setup]
AppId={{B7E3F2A1-4C5D-4E8F-9A2B-1D3C5E7F9A0B}
AppName=Telefonerkennung
AppVersion={#MyAppVersion}
AppPublisher=Telefonerkennung

; Installation systemweit (Program Files), mit Admin-Rechten
DefaultDirName={pf}\Telefonerkennung
DefaultGroupName=Telefonerkennung
PrivilegesRequired=admin

OutputDir=installer
OutputBaseFilename=Telefonerkennung-Setup-{#MyAppVersion}

SetupIconFile=telefon.ico
UninstallDisplayIcon={app}\Telefonerkennung.exe

Compression=lzma2
SolidCompression=yes
WizardStyle=modern

CloseApplications=yes
CloseApplicationsFilter=Telefonerkennung.exe
RestartApplications=no

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Files]
; --onedir build: gesamter Ordnerinhalt installieren
Source: "dist\Telefonerkennung\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Telefonerkennung"; Filename: "{app}\Telefonerkennung.exe"; IconFilename: "{app}\Telefonerkennung.exe"
Name: "{autodesktop}\Telefonerkennung"; Filename: "{app}\Telefonerkennung.exe"; IconFilename: "{app}\Telefonerkennung.exe"

[Registry]
; Autostart systemweit (für alle Benutzer).
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Telefonerkennung"; ValueData: """{app}\Telefonerkennung.exe"""; Flags: uninsdeletevalue

[Run]
; App nach Installation starten – AUCH im Silent-Modus.
; runasoriginaluser: App als Original-User starten (nicht als Admin).
; OHNE skipifsilent: Eintrag wird AUCH im Silent-Modus ausgeführt.
Filename: "{app}\Telefonerkennung.exe"; Flags: nowait postinstall runasoriginaluser

[Code]
const
  // Deinstallations-Schlüssel unserer AppId ({{ verdoppelt = literales {).
  AppUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{B7E3F2A1-4C5D-4E8F-9A2B-1D3C5E7F9A0B}_is1';

procedure CleanOrphanedPerUserEntry();
begin
  // Wir installieren immer per-machine (HKLM). Ein Eintrag im
  // Benutzer-Hive (HKCU) stammt aus der alten Per-User-Ära und zeigt
  // dauerhaft eine veraltete Version an – bei jedem Update entfernen.
  if RegKeyExists(HKCU, AppUninstallKey) then
  begin
    RegDeleteKeyIncludingSubkeys(HKCU, AppUninstallKey);
  end;
end;

function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  // App gnadenlos killen VOR Installation
  Exec('cmd.exe', '/C taskkill /F /IM Telefonerkennung.exe 2>nul', '', SW_HIDE, ewNoWait, ResultCode);
  // Verwaisten Per-User-Deinstallations-Eintrag entfernen
  CleanOrphanedPerUserEntry();
  Result := True;
end;