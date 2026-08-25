#define MyAppName "逃费车辆信息智能查询平台"
#define MyAppExeName "逃费车辆信息智能查询平台.exe"
#define MyAppId "F9B73C99-E62D-4DB4-8C5A-CE64DEB5609D"
#ifndef MyAppVersion
  #define MyAppVersion "1.1.2"
#endif
#ifndef StageDir
  #define StageDir "..\dist\installer-stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif
#ifndef LanguageFile
  #define LanguageFile "compiler:Default.isl"
#endif
#ifndef SetupIcon
  #define SetupIcon "..\integrated_client\ui\assets\app-icon.ico"
#endif
#define ShortcutIconName "IntDemoOnline-icon-" + MyAppVersion + ".ico"

[Setup]
AppId={{{#MyAppId}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=IntDemo
AppPublisherURL=https://github.com/e23Aqiu/intdemo
DefaultDirName={localappdata}\Programs\IntDemoOnline
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
#ifdef PatchMode
OutputBaseFilename=IntDemoOnline-Patch-{#PatchFromVersion}-to-{#MyAppVersion}
#else
OutputBaseFilename=IntDemoOnline-Setup-{#MyAppVersion}
#endif
; Fast, non-solid compression installs much faster and lets patch packages
; contain only files changed since the previous release.
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
ChangesAssociations=yes
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#ShortcutIconName}
VersionInfoCompany=IntDemo
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
SetupIconFile={#SetupIcon}

[Languages]
Name: "chinesesimplified"; MessagesFile: "{#LanguageFile}"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[InstallDelete]
Type: files; Name: "{app}\IntDemoOnline-icon-*.ico"
; Remove files and shortcuts left by releases that used the previous display name.
Type: files; Name: "{app}\逃费车辆智能查询平台.exe"
Type: files; Name: "{autoprograms}\逃费车辆智能查询平台.lnk"
Type: files; Name: "{autodesktop}\逃费车辆智能查询平台.lnk"
Type: files; Name: "{app}\营运信息批量查询工具.exe"
Type: files; Name: "{autoprograms}\营运信息批量查询工具.lnk"
Type: files; Name: "{autodesktop}\营运信息批量查询工具.lnk"
#ifdef PatchMode
  #ifdef PatchDeleteFile
    #include PatchDeleteFile
  #endif
#else
Type: filesandordirs; Name: "{app}\_internal"
#endif

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SetupIcon}"; DestDir: "{app}"; DestName: "{#ShortcutIconName}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#ShortcutIconName}"; IconIndex: 0
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#ShortcutIconName}"; IconIndex: 0; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
const
  SHCNE_ASSOCCHANGED = $08000000;
  SHCNF_IDLIST = $0000;
  SHCNF_FLUSHNOWAIT = $2000;

procedure SHChangeNotify(
  wEventId: LongWord;
  uFlags: LongWord;
  dwItem1: Integer;
  dwItem2: Integer
);
  external 'SHChangeNotify@shell32.dll stdcall';

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SHChangeNotify(
      SHCNE_ASSOCCHANGED,
      SHCNF_IDLIST or SHCNF_FLUSHNOWAIT,
      0,
      0
    );
end;

#ifdef PatchMode
function InitializeSetup(): Boolean;
var
  InstalledVersion: String;
  UninstallKey: String;
#ifdef FullInstallerUrl
  ErrorCode: Integer;
#endif
begin
  UninstallKey :=
    'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{#MyAppId}}_is1';
  Result :=
    (RegQueryStringValue(
      HKCU, UninstallKey, 'DisplayVersion', InstalledVersion
    ) or RegQueryStringValue(
      HKLM, UninstallKey, 'DisplayVersion', InstalledVersion
    )) and (InstalledVersion = '{#PatchFromVersion}');
  if not Result then
#ifdef FullInstallerUrl
  begin
    MsgBox(
      '此增量更新包仅适用于 {#PatchFromVersion}。' + #13#10 +
      '当前未检测到对应安装版本，将为你打开完整安装包下载地址。',
      mbError,
      MB_OK
    );
    ShellExec(
      'open',
      '{#FullInstallerUrl}',
      '',
      '',
      SW_SHOWNORMAL,
      ewNoWait,
      ErrorCode
    );
  end;
#else
    MsgBox(
      '此增量更新包仅适用于 {#PatchFromVersion}。' + #13#10 +
      '当前未检测到对应版本，请联系管理员获取完整安装包。',
      mbError,
      MB_OK
    );
#endif
end;
#endif
