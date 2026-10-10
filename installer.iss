#ifndef MyAppVersion
#define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{B7E3F2A1-4C5D-4E8F-9A2B-1D3C5E7F9A0B}
AppName=Telefonerkennung
AppVersion={#MyAppVersion}
AppPublisher=Telefonerkennung
DefaultDirName={autopf}\Telefonerkennung
DefaultGroupName=Telefonerkennung
OutputDir=installer
OutputBaseFilename=Telefonerkennung-Setup-{#MyAppVersion}
SetupIconFile=telefon.ico
UninstallDisplayIcon={app}\Telefonerkennung.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
CloseApplications=yes
; RestartApplications aus: die [Code]-Sektion startet die App
; nach der Installation selbst – sonst waeren das zwei Starts.
RestartApplications=no

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Files]
Source: "dist\Telefonerkennung.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Telefonerkennung"; Filename: "{app}\Telefonerkennung.exe"; IconFilename: "{app}\Telefonerkennung.exe"
Name: "{autodesktop}\Telefonerkennung"; Filename: "{app}\Telefonerkennung.exe"; IconFilename: "{app}\Telefonerkennung.exe"

[Registry]
; Autostart mit Windows (pro Benutzer).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Telefonerkennung"; ValueData: """{app}\Telefonerkennung.exe"""; Flags: uninsdeletevalue

[Code]
// App nach Installation IMMER starten – auch im Silent-Modus.
// RestartApplications ist aus, und es gibt bewusst keinen [Run]-Eintrag,
// sonst waeren das zwei Starts.
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    Exec(ExpandConstant('{app}\Telefonerkennung.exe'), '', '', SW_SHOW, ewNoWait, ResultCode);
  end;
end;
