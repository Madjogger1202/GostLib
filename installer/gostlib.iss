; ---------------------------------------------------------------------------
; GostLib -- установщик для Windows (Inno Setup 6.3 или новее).
;
; Собирать через installer\build_installer.py: он проверит наличие
; dist\GostLib.exe, найдёт ISCC.exe и подставит версию из gostlib/__init__.py
; ключом /DAppVersion=...  Скомпилировать вручную тоже можно:
;     ISCC.exe installer\gostlib.iss
; тогда возьмётся версия по умолчанию, объявленная ниже.
;
; Файл сохранён в UTF-8 С BOM. Без BOM компилятор Inno Setup читает .iss в
; ANSI-кодировке системы, и русские строки в [CustomMessages] превращаются
; в мусор.
; ---------------------------------------------------------------------------

#define AppName        "GostLib"
#define AppPublisher   "GostLib"
#define AppExeName     "GostLib.exe"
#define AppIcoName     "gostlib.ico"

; Единственное место, где правится версия вручную. #ifndef нужен потому,
; что build_installer.py передаёт версию ключом /DAppVersion -- значение с
; командной строки объявляется раньше и должно иметь приоритет.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
; AppId постоянный и не меняется никогда: по нему Windows понимает, что
; новая версия -- это обновление, а не вторая копия программы.
AppId={{5B84CAAB-BFC5-4FDC-8CB3-80E7BB1BE637}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}

DefaultDirName={autopf}\{#AppName}
; Ярлык кладём прямо в меню Пуск ({autoprograms}), папку-группу не заводим,
; поэтому страница выбора группы в мастере лишняя.
DisableProgramGroupPage=yes
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}

; Ставим на всех пользователей, но даём выбрать установку «только для меня»
; -- без прав администратора, в {localappdata}\Programs. {autopf} и {autodesktop}
; сами разворачиваются в системный или пользовательский вариант.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

; PySide6 не поддерживает ничего старше Windows 10, а exe собирается 64-битным.
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

OutputDir=..\dist
OutputBaseFilename=GostLib-Setup-{#AppVersion}
SetupIconFile=..\gostlib\gui\{#AppIcoName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

; GostLib.exe -- один файл на несколько десятков мегабайт: внутри Python и
; Qt. Перезаписать его поверх запущенной копии Windows не даст, поэтому при
; обновлении просим закрыть программу, а не падаем с ошибкой доступа.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
; %1 -- полный путь к рабочей папке, подставляется через FmtMessage в [Code].
russian.RemoveUserData=Удалить рабочую папку GostLib?%n%n%1%n%nТам лежат каталог компонентов, настройки и резервные копии. Если программа будет установлена заново, ответьте «Нет» -- все компоненты сохранятся.
english.RemoveUserData=Delete the GostLib working folder?%n%n%1%n%nIt holds the component catalog, settings and backups. If you are going to reinstall GostLib, answer "No" -- everything will be kept.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; Иконка ставится отдельным файлом, чтобы ярлыки ссылались на неё напрямую:
; вытаскивать значок из 60-мегабайтного onefile-exe проводник умеет, но
; делает это медленно и иногда кэширует старую картинку.
Source: "..\gostlib\gui\{#AppIcoName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppIcoName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppIcoName}"; Tasks: desktopicon

[Registry]
; Только HKCU: в системные ветки не пишем, чтобы установка «только для меня»
; работала без администратора и ничего за собой не оставляла.
Root: HKCU; Subkey: "Software\{#AppName}"; Flags: uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\{#AppName}"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\{#AppName}"; ValueType: string; ValueName: "Version"; ValueData: "{#AppVersion}"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Рабочие данные лежат в %LOCALAPPDATA%\GostLib и здесь не упоминаются
; намеренно: удалять каталог компонентов молча нельзя. Спрашиваем о нём
; отдельным вопросом в [Code]. Здесь -- только сама папка программы, если
; после удаления файлов в ней ничего не осталось.
Type: dirifempty; Name: "{app}"

[Code]

function UserDataDir(): String;
begin
  { Тот же путь, что считает gostlib/config.py: %LOCALAPPDATA%\GostLib. }
  Result := ExpandConstant('{localappdata}\GostLib');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dir: String;
begin
  { Спрашиваем после того, как программа уже удалена: если пользователь
    передумает на середине, файлы каталога останутся нетронутыми. }
  if CurUninstallStep <> usPostUninstall then
    Exit;

  Dir := UserDataDir();
  if not DirExists(Dir) then
    Exit;

  { SuppressibleMsgBox с ответом IDNO по умолчанию: при тихом удалении
    (/SILENT в корпоративном развёртывании) данные обязаны уцелеть.
    Кнопка «Нет» выбрана заранее -- ошибиться сложнее. }
  if SuppressibleMsgBox(FmtMessage(CustomMessage('RemoveUserData'), [Dir]),
                        mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
    DelTree(Dir, True, True, True);
end;
