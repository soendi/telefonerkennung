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
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Files]
Source: "dist\Telefonerkennung.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Telefonerkennung"; Filename: "{app}\Telefonerkennung.exe"
Name: "{autodesktop}\Telefonerkennung"; Filename: "{app}\Telefonerkennung.exe"

[Run]
Filename: "{app}\Telefonerkennung.exe"; Description: "Telefonerkennung jetzt starten"; Flags: nowait postinstall skipifsilent
