; VoiceType - proper Windows installer wizard (Inno Setup).
; 100% free, open-source, no code-signing required.
; Built by build-release.ps1, which stages the app and passes AppSource/OutputDir:
;   ISCC /DAppSource=<staged app> /DOutputDir=<dist> installer\voicetype.iss

#ifndef AppSource
  #define AppSource "..\dist\stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#define AppName "VoiceType"
#define AppVersion "1.0.0"

[Setup]
; A stable, unique ID so upgrades replace the previous version cleanly.
AppId={{8F2B1C40-9A7E-4B3D-AE12-5C9D7E1F0A33}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=VoiceType (free & open source)
DefaultDirName={localappdata}\Programs\VoiceType
DisableProgramGroupPage=yes
DisableDirPage=yes
; Per-user install: no administrator prompt needed.
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=VoiceType-Setup
SetupIconFile={#AppSource}\voicetype\assets\voicetype.ico
UninstallDisplayIcon={app}\voicetype\assets\voicetype.ico
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
LicenseFile={#AppSource}\LICENSE
ArchitecturesInstallIn64BitMode=x64compatible

[Messages]
WelcomeLabel2=This will install [name] on your computer.%n%nVoiceType is a free, offline talk-to-type app. Setup will download Python, the speech engine, and the speech model the first time (a few minutes, ~1 GB). After that it runs fully offline.

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Run]
; The real setup: build the isolated environment, install dependencies, detect an
; NVIDIA GPU, download the model, and create shortcuts + auto-start.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1"" -StartOnLogin -Desktop"; \
  WorkingDir: "{app}"; \
  StatusMsg: "Setting up VoiceType - downloading Python, the speech engine, and the model. This can take a few minutes..."; \
  Flags: waituntilterminated
; Optional: launch it right away.
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-m voicetype"; \
  Description: "Launch VoiceType now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; Clean up the env + shortcuts before the files are removed.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\uninstall.ps1"""; \
  WorkingDir: "{app}"; Flags: runhidden; RunOnceId: "VoiceTypeUninstall"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
