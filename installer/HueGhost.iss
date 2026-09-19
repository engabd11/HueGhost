; Hue Ghost - Windows installer (Inno Setup 6)
; Build: scripts\build_installer.ps1   (builds the app, fetches mpv + the virtual display driver, runs ISCC)

#define AppName "Hue Ghost"
#define AppVersion GetEnv("HUEGHOST_VERSION")
#if AppVersion == ""
  #define AppVersion "2.1.1"
#endif
#define AppPublisher "engabd11"
#define AppURL "https://github.com/engabd11/HueGhost"

[Setup]
AppId={{7D5E2C9A-4B1F-4E7A-9C33-5A0B8C2D1E4F}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\Hue Ghost
DefaultGroupName=Hue Ghost
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist\installer
OutputBaseFilename=HueGhost-Setup-{#AppVersion}
SetupIconFile=hueghost.ico
UninstallDisplayIcon={app}\HueGhost.exe
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Types]
Name: "full"; Description: "Full installation (recommended)"
Name: "custom"; Description: "Custom installation"; Flags: iscustom

[Components]
Name: "app"; Description: "Hue Ghost app and command line"; Types: full custom; Flags: fixed
Name: "mpv"; Description: "mpv video player (plays the ghost; bundled, GPL)"; Types: full custom
Name: "vdd"; Description: "Virtual display for the ghost - keeps your real screen free (signed driver by VirtualDrivers)"; Types: full custom

[Tasks]
Name: "autostart"; Description: "Start Hue Ghost when I sign in (minimised to the tray)"; GroupDescription: "Startup:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\HueGhost\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion; Components: app
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion; Components: app
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion; Components: app
Source: "..\docs\*.md"; DestDir: "{app}\docs"; Flags: ignoreversion; Components: app
Source: "payload\mpv\*"; DestDir: "{app}\mpv"; Flags: recursesubdirs ignoreversion; Components: mpv
Source: "payload\vdd\*"; DestDir: "{app}\vdd"; Flags: recursesubdirs ignoreversion; Components: vdd
Source: "payload\vdd\vdd_settings.xml"; DestDir: "C:\VirtualDisplayDriver"; Flags: onlyifdoesntexist uninsneveruninstall; Components: vdd

[Icons]
Name: "{group}\Hue Ghost"; Filename: "{app}\HueGhost.exe"; Comment: "Software Hue Sync Box for Jellyfin"
Name: "{group}\Hue Ghost documentation"; Filename: "{app}\README.md"
Name: "{group}\Uninstall Hue Ghost"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Hue Ghost"; Filename: "{app}\HueGhost.exe"; Tasks: desktopicon

[Run]
Filename: "{sys}\pnputil.exe"; Parameters: "/add-driver ""{app}\vdd\MttVDD.inf"" /install"; Components: vdd; Check: not VddPresent; Flags: runhidden waituntilterminated; StatusMsg: "Installing the virtual display driver..."
Filename: "{app}\vdd\devcon.exe"; Parameters: "install ""{app}\vdd\MttVDD.inf"" Root\MttVDD"; Components: vdd; Check: not VddPresent; Flags: runhidden waituntilterminated; StatusMsg: "Creating the virtual display..."
Filename: "{app}\HueGhost.exe"; Parameters: "install-autostart"; Tasks: autostart; Flags: runhidden waituntilterminated runasoriginaluser; StatusMsg: "Registering startup..."
Filename: "{app}\HueGhost.exe"; Description: "Launch Hue Ghost now"; Flags: postinstall nowait runasoriginaluser skipifsilent

[UninstallRun]
Filename: "{app}\HueGhost.exe"; Parameters: "uninstall-autostart"; Flags: runhidden waituntilterminated; RunOnceId: "hg-autostart"
Filename: "{app}\vdd\devcon.exe"; Parameters: "remove Root\MttVDD"; Components: vdd; Flags: runhidden waituntilterminated; RunOnceId: "hg-vdd"

[Code]
// A virtual display created from this driver shows up as ROOT\DISPLAY\000N with HardwareID Root\MttVDD.
function VddPresent(): Boolean;
var
  i: Integer;
  key, ids: String;
begin
  Result := False;
  for i := 0 to 15 do
  begin
    key := 'SYSTEM\CurrentControlSet\Enum\ROOT\DISPLAY\' + Format('%.4d', [i]);
    if RegQueryMultiStringValue(HKLM, key, 'HardwareID', ids) then
      if Pos('mttvdd', Lowercase(ids)) > 0 then
      begin
        Result := True;
        exit;
      end;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpSelectComponents then
    if VddPresent() then
      WizardForm.ComponentsList.ItemCaption[2] := WizardForm.ComponentsList.ItemCaption[2] + ' (already installed)';
end;
