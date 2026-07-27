#define MyAppName "营运信息批量查询工具"
#define MyAppExeName "营运信息批量查询工具.exe"
#ifndef MyAppVersion
  #define MyAppVersion "0.2.2"
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

[Setup]
AppId={{F9B73C99-E62D-4DB4-8C5A-CE64DEB5609D}
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
OutputBaseFilename=IntDemoOnline-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
VersionInfoCompany=IntDemo
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}

[Languages]
Name: "chinesesimplified"; MessagesFile: "{#LanguageFile}"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
