#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#ifndef StageDir
  #define StageDir "..\build\installer-stage"
#endif

[Setup]
AppId={{7DE8DC24-BD9C-4D70-8020-E8C8EFA06D76}
AppName=Real-time Multilingual Subtitles
AppVersion={#AppVersion}
AppPublisher=grzhx
AppPublisherURL=https://github.com/grzhx/realtime-multilingual-subtitles
DefaultDirName={localappdata}\Programs\RealTimeMultilingualSubtitles
DefaultGroupName=Real-time Multilingual Subtitles
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\dist
OutputBaseFilename=realtime-multilingual-subtitles-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE
DisableProgramGroupPage=yes
UninstallDisplayName=Real-time Multilingual Subtitles
UninstallDisplayIcon={app}\runtime\python\pythonw.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "config.toml"
Source: "{#StageDir}\config.toml"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\Real-time Multilingual Subtitles"; Filename: "{app}\runtime\python\pythonw.exe"; Parameters: """{app}\launch.py"""; WorkingDir: "{app}"
Name: "{autodesktop}\Real-time Multilingual Subtitles"; Filename: "{app}\runtime\python\pythonw.exe"; Parameters: """{app}\launch.py"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\python\pythonw.exe"; Parameters: """{app}\launch.py"""; WorkingDir: "{app}"; Description: "Initialize and start subtitles (downloads dependencies and models)"; Flags: nowait postinstall skipifsilent unchecked
