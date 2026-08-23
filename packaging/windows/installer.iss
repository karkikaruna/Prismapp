; Inno Setup script - builds PRISM-Setup-<version>.exe from dist\PRISM (PyInstaller onedir).
; Build with: iscc packaging\windows\installer.iss /DMyAppVersion=1.0.0
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

[Setup]
AppId={{7C5B2B1A-8B0E-4E9E-9C8D-4B2C7A9E6F31}}
AppName=PRISM
AppVersion={#MyAppVersion}
AppPublisher=PRISM
DefaultDirName={autopf}\PRISM
DefaultGroupName=PRISM
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=PRISM-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\PRISM.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "..\..\dist\PRISM\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PRISM"; Filename: "{app}\PRISM.exe"
Name: "{autodesktop}\PRISM"; Filename: "{app}\PRISM.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PRISM.exe"; Description: "Launch PRISM"; Flags: nowait postinstall skipifsilent