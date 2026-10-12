; Inno Setup 安装脚本（6.5 以上）。简体中文界面用同目录的 ChineseSimplified.isl（取自 Inno Setup 官方仓库）。
; 本文件和 .isl 都用带 BOM 的 UTF-8 保存，Inno Setup 才不会按本地代码页读错中文。
; 用法：iscc /DAppVersion=0.2.0 /DBuild=42 packaging\bidbench.iss
; 装到当前用户目录，不需要管理员权限，所以更新时可以静默安装、不弹 UAC。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Build
  #define Build "0"
#endif
#define AppName "投标图纸工作台"

[Setup]
AppId={{8C4F3E9A-6B1D-4F2E-9A7C-5D3B2E1F0A47}
AppName={#AppName}
AppVersion={#AppVersion}.{#Build}
AppVerName={#AppName} {#AppVersion}（构建 b{#Build}）
AppPublisher=bidbench
VersionInfoVersion={#AppVersion}.{#Build}
DefaultDirName={localappdata}\Programs\bidbench
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=bidbench-setup-b{#Build}
SetupIconFile=build\bidbench.ico
UninstallDisplayIcon={app}\bidbench-desktop.exe
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
ChangesEnvironment=yes

[Languages]
Name: "chs"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "在桌面创建快捷方式"
Name: "addpath"; Description: "把 bidbench 命令加入 PATH（命令行和 AI 助手 MCP 用）"

[InstallDelete]
; 先清掉旧版本的库，避免新旧文件混在一起
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\bidbench\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\bidbench-desktop.exe"
Name: "{group}\卸载{#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\bidbench-desktop.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; \
  Tasks: addpath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
; 静默安装（一键更新）时也会执行：装完自动重新打开工作台
Filename: "{app}\bidbench-desktop.exe"; Description: "打开{#AppName}"; Flags: nowait postinstall

[Code]
function NeedsAddPath(Dir: string): Boolean;
var
  Paths: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Paths) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') = 0;
end;

procedure RemovePath(Dir: string);
var
  Paths: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Paths) then
    exit;
  P := Pos(';' + Uppercase(Dir), Uppercase(Paths));
  if P > 0 then
  begin
    Delete(Paths, P, Length(Dir) + 1);
    RegWriteExpandStringValue(HKCU, 'Environment', 'Path', Paths);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemovePath(ExpandConstant('{app}'));
end;
