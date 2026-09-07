{ ===========================================================================
  GostLibBuilder v3 -- сборка библиотеки GostLib средствами самого Altium.

  Утилита GostLib готовит текстовое задание (build_*.txt), а этот скрипт
  строит по нему .SchLib и .PcbLib штатным API (ISch_Lib / IPCB_Library).
  Ничего двоичного руками не пишется, поэтому «File corrupted» невозможен
  в принципе, 3D-модели реально вшиваются в посадочные места, а имена
  длиной больше 31 знака не ломаются.

  Запуск:  DXP -> Run Script -> GostLibBuilder -> RunGostLib
  Кнопкой: DXP -> Customize -> Scripts, перетащить RunGostLib на панель.

  Диагностика:
     GostLibWhereIsJob      -- где скрипт ищет задание
     GostLibCheckFile       -- прочитать задание и показать первые строки
                               (заодно видно, не поехала ли кириллица)
     GostLibListInstalled   -- какие библиотеки подключены

  Формат задания -- строки «тег<TAB>поле<TAB>поле...»:

     VER      3
     LOG      <путь к журналу>
     SCHLIB   <путь к .SchLib>
     PCBLIB   <путь к .PcbLib>
     FONT     <имя шрифта>
     INSTALL  0|1
     FRESH    0|1          1 = библиотеки строятся с нуля
     PINLOC   0|1          1 = Location вывода -- электрический конец,
                           0 = конец у корпуса (проверено: верно 0)
     COLGRAPH n            цвет графики (Altium: целое BGR, 0 = чёрный)
     COLTEXT  n            цвет текста
     COLPIN   n            цвет линий выводов
     HIDECMT  0|1          1 = спрятать штатный Comment (тип рисуем сами)
     INTLIB   0|1          1 = собрать ещё и .IntLib из .LibPkg
     LIBPKG   <путь к .LibPkg, его пишет GostLib>
     VENDOR   <путь к чужой .PcbLib -- только подключить>

     FP    имя  описание  высота
     PAD   номер x y w h форма поворот*10 слой отверстие металлиз длина_паза угол_паза*10
     TRK   слой ширина x1 y1 x2 y2
     FARC  слой ширина cx cy r угол1*10 угол2*10
     FTXT  слой x y высота поворот*10 ширина зеркало текст
     FILL  слой x1 y1 x2 y2
     BODY  путь_к_step dx dy standoff rx*10 ry*10 rz*10
     ENDFP

     COMP   имя обозначение описание комментарий частей
     DESIG  x y
     CMTPOS x y
     PARAM  имя значение скрытый
     FPREF  имя_посадки имя_чужой_библиотеки
     PIN    часть номер имя эл_тип x y длина поворот флаги xэл yэл
     SLINE  часть толщина x1 y1 x2 y2 [цвет]
     SRECT  часть толщина x1 y1 x2 y2 залит
     SARC   часть толщина cx cy r угол1*10 угол2*10
     SELL   часть толщина cx cy r залит
     SPOLY  часть толщина залит N x1 y1 ...
     STEXT  часть x y кегль поворот выключка жирный текст
     ENDCOMP

     END   компонентов посадок

  Все координаты -- во внутренних единицах Altium (1/10000 мила), углы --
  в десятых долях градуса. Дробных чисел в задании нет вообще: на русской
  Windows StrToFloat ждёт запятую и споткнулся бы на «1.27».
  =========================================================================== }

Const
    { Путь к файлу-указателю подставляет GostLib при запуске. }
    PTR_PATH     = '@@POINTER@@';
    PTR_FALLBACK = 'C:\GostLib\current_job.txt';

    JOB_VERSION  = 3;

Var
    GLog      : TStringList;
    GLogPath  : String;
    GJob      : TStringList;
    GVendor   : TStringList;
    GMadeSch  : TStringList;
    GMadePcb  : TStringList;
    GSchPath  : String;
    GPcbPath  : String;
    GFont     : String;
    GInstall  : Integer;
    GFresh    : Integer;
    GPinLoc   : Integer;
    GIntLib   : Integer;
    GColG     : Integer;
    GColT     : Integer;
    GColP     : Integer;
    GHideCmt  : Integer;
    GLibPkg   : String;
    GIntPath  : String;
    GErr      : Integer;
    GWarn     : Integer;
    GNComp    : Integer;
    GNFp      : Integer;
    GNPad     : Integer;
    GNBody    : Integer;
    GNPin     : Integer;
{ ============================================================== журнал ==== }

Procedure FlushLog;
Begin
    If (GLog = Nil) Or (GLogPath = '') Then Exit;
    Try
        GLog.SaveToFile(GLogPath);
    Except
    End;
End;

Procedure Say(S : String);
Begin
    If GLog <> Nil Then GLog.Add(S);
    FlushLog;
End;

Procedure SayErr(S : String);
Begin
    GErr := GErr + 1;
    Say('ОШИБКА: ' + S);
End;

Procedure SayWarn(S : String);
Begin
    GWarn := GWarn + 1;
    Say('  внимание: ' + S);
End;

{ ============================================================== разбор ==== }

{ N-е поле строки, поля разделены табуляцией, нумерация с единицы. }
Function Fld(S : String; N : Integer) : String;
Var
    i, cnt, start : Integer;
Begin
    Result := '';
    cnt := 1;
    start := 1;
    For i := 1 To Length(S) Do
        If S[i] = #9 Then
        Begin
            If cnt = N Then
            Begin
                Result := Copy(S, start, i - start);
                Exit;
            End;
            cnt := cnt + 1;
            start := i + 1;
        End;
    If cnt = N Then Result := Copy(S, start, Length(S) - start + 1);
End;

Function FldI(S : String; N : Integer) : Integer;
Begin
    Result := StrToIntDef(Trim(Fld(S, N)), 0);
End;

{ Цвет из поля N. Пусто или отрицательное -- берём общий цвет графики. }
Function FldCol(S : String; N : Integer; D : Integer) : Integer;
Var
    v : Integer;
Begin
    v := StrToIntDef(Trim(Fld(S, N)), -1);
    If v < 0 Then Result := D Else Result := v;
End;

Function Tag(S : String) : String;
Begin
    Result := Fld(S, 1);
End;

{ Раскрыть экранирование: \\ \t \n \uXXXX. }
Function Unesc(S : String) : String;
Var
    i         : Integer;
    code      : Integer;
    ch   : String;
Begin
    Result := '';
    i := 1;
    While i <= Length(S) Do
    Begin
        If (S[i] = '\') And (i < Length(S)) Then
        Begin
            ch := S[i + 1];
            If ch = 'n' Then
            Begin
                Result := Result + #13#10;
                i := i + 2;
            End
            Else If ch = 't' Then
            Begin
                Result := Result + #9;
                i := i + 2;
            End
            Else If ch = '\' Then
            Begin
                Result := Result + '\';
                i := i + 2;
            End
            Else If (ch = 'u') And (i + 5 <= Length(S)) Then
            Begin
                code := StrToIntDef('$' + Copy(S, i + 2, 4), 63);
                Try
                    Result := Result + Chr(code);
                Except
                    Result := Result + '?';
                End;
                i := i + 6;
            End
            Else
            Begin
                Result := Result + S[i];
                i := i + 1;
            End;
        End
        Else
        Begin
            Result := Result + S[i];
            i := i + 1;
        End;
    End;
End;

Function FldS(S : String; N : Integer) : String;
Begin
    Result := Unesc(Fld(S, N));
End;

Function PointerPath : String;
Begin
    If (PTR_PATH <> '') And (Pos('@@', PTR_PATH) = 0) Then Result := PTR_PATH
    Else Result := PTR_FALLBACK;
End;

Function FindJobFile : String;
Var
    ptr : String;
    L   : TStringList;
Begin
    Result := '';
    ptr := PointerPath;
    If Not FileExists(ptr) Then
    Begin
        ptr := PTR_FALLBACK;
        If Not FileExists(ptr) Then Exit;
    End;
    L := TStringList.Create;
    Try
        L.LoadFromFile(ptr);
        If L.Count > 0 Then Result := Trim(L[0]);
    Finally
        L.Free;
    End;
End;

{ =========================================================== документы ==== }

Procedure CloseIfOpen(Path : String);
Var
    SrvDoc;
Begin
    If Client = Nil Then Exit;
    SrvDoc := Nil;
    Try
        SrvDoc := Client.GetDocumentByPath(Path);
    Except
        SrvDoc := Nil;
    End;
    If SrvDoc = Nil Then Exit;
    Try
        { чтобы не спрашивал «сохранить изменения?» -- мы всё равно
          перестраиваем библиотеку заново }
        SrvDoc.Modified := False;
    Except
    End;
    Try
        Client.CloseDocument(SrvDoc);
        Say('  закрыт в Altium: ' + ExtractFileName(Path));
    Except
        SayWarn('не удалось закрыть ' + ExtractFileName(Path));
    End;
End;

{ Сохранить предыдущую версию рядом как .bak -- на случай «а верните как было». }
Procedure Backup(Path : String);
Var
    bak : String;
Begin
    If Not FileExists(Path) Then Exit;
    bak := Path + '.bak';
    Try
        If FileExists(bak) Then DeleteFile(bak);
        CopyFile(Path, bak, False);
    Except
    End;
End;

Function AlreadyInstalled(Path : String) : Boolean;
Var
    i         : Integer;
Begin
    Result := False;
    If IntegratedLibraryManager = Nil Then Exit;
    Try
        For i := 0 To IntegratedLibraryManager.AvailableLibraryCount - 1 Do
            If UpperCase(IntegratedLibraryManager.InstalledLibraryPath(i)) =
               UpperCase(Path) Then Result := True;
    Except
        Result := False;
    End;
End;

{ Заставить Altium перечитать список библиотек: без этого панель
  Components показывает то, что было на момент установки. }
Procedure RefreshLibraries;
Begin
    Try
        ResetParameters;
        RunProcess('IntegratedLibrary:RefreshInstalledLibraries');
    Except
    End;
End;

Procedure UninstallLib(Path : String);
Begin
    If IntegratedLibraryManager = Nil Then Exit;
    Try
        IntegratedLibraryManager.UninstallLibrary(Path);
    Except
    End;
End;

Procedure InstallLib(Path : String);
Begin
    If Not FileExists(Path) Then Exit;
    If IntegratedLibraryManager = Nil Then
    Begin
        SayWarn('менеджер библиотек недоступен, подключите вручную: ' + Path);
        Exit;
    End;
    If AlreadyInstalled(Path) Then
        Try
            { переподключаем, чтобы Altium перечитал файл с диска }
            IntegratedLibraryManager.UninstallLibrary(Path);
        Except
        End;
    Try
        IntegratedLibraryManager.InstallLibrary(Path);
        Say('  подключена: ' + ExtractFileName(Path));
    Except
        SayWarn('подключить не вышло, сделайте вручную (Components -> ' +
                'File-based Libraries Preferences -> Install): ' + Path);
    End;
End;

{ ========================================================= слои и формы ==== }

Function PcbLayerOf(Code : Integer) : Integer;
Begin
    Case Code Of
        1 : Result := eTopOverlay;
        2 : Result := eBottomOverlay;
        3 : Result := eMechanical13;      { сборочный чертёж }
        4 : Result := eMechanical15;      { область установки (courtyard) }
        5 : Result := eKeepOutLayer;
        6 : Result := eMechanical1;
        7 : Result := eTopLayer;
        8 : Result := eBottomLayer;
        9 : Result := eTopPaste;
        10: Result := eTopSolder;
    Else
        Result := eTopOverlay;
    End;
End;

Function PadLayerOf(Code : Integer) : Integer;
Begin
    Case Code Of
        2 : Result := eBottomLayer;
        3 : Result := eMultiLayer;
    Else
        Result := eTopLayer;
    End;
End;

Function PadShapeOf(Code : Integer) : Integer;
Begin
    Case Code Of
        1 : Result := eRounded;
        3 : Result := eRoundedRectangular;
        4 : Result := eOctagonal;
    Else
        Result := eRectangular;
    End;
End;

Procedure UseLayer(Lib, Code);
Begin
    If Lib = Nil Then Exit;
    Try
        Lib.Board.LayerIsUsed[PcbLayerOf(Code)] := True;
    Except
    End;
End;

{ ====================================================== сборка .PcbLib ==== }

Procedure AddPad(Comp, line);
Var
    Pad;
    shp       : Integer;
    sz        : Integer;
    hl        : Integer;
Begin
    Pad := PCBServer.PCBObjectFactory(ePadObject, eNoDimension, eCreate_Default);
    If Pad = Nil Then
    Begin
        SayErr('не создалась площадка ' + Fld(line, 2));
        Exit;
    End;
    Try
        Pad.Mode := ePadMode_Simple;
    Except
    End;
    shp := PadShapeOf(FldI(line, 7));
    Pad.X := FldI(line, 3);
    Pad.Y := FldI(line, 4);
    Pad.TopXSize := FldI(line, 5);
    Pad.TopYSize := FldI(line, 6);
    Pad.MidXSize := FldI(line, 5);
    Pad.MidYSize := FldI(line, 6);
    Pad.BotXSize := FldI(line, 5);
    Pad.BotYSize := FldI(line, 6);
    Pad.TopShape := shp;
    Pad.MidShape := shp;
    Pad.BotShape := shp;
    Pad.Rotation := FldI(line, 8) / 10.0;
    Pad.Layer := PadLayerOf(FldI(line, 9));
    hl := FldI(line, 10);
    If hl > 0 Then
    Begin
        Pad.HoleSize := hl;
        Try
            Pad.Plated := (FldI(line, 11) = 1);
        Except
        End;
        sz := FldI(line, 12);
        If sz > hl Then
            Try
                Pad.HoleType := eSlotHole;
                Pad.HoleWidth := sz;
                { Без угла паз всегда ложится вдоль X: вертикальные пазы
                  крепёжных площадок выходили поперёк. Свойство трогаем
                  только когда угол реально задан -- если у этой сборки
                  Altium его нет, обычные пазы всё равно построятся. }
                If FldI(line, 13) <> 0 Then
                    Try
                        Pad.HoleRotation := FldI(line, 13) / 10.0;
                    Except
                        SayWarn('поворот паза не поддержан этой сборкой Altium');
                    End;
            Except
            End;
    End;
    Pad.Name := FldS(line, 2);
    Comp.AddPCBObject(Pad);
    GNPad := GNPad + 1;
End;

Procedure AddTrack(Comp, Lib, line);
Var
    Trk;
Begin
    UseLayer(Lib, FldI(line, 2));
    Trk := PCBServer.PCBObjectFactory(eTrackObject, eNoDimension, eCreate_Default);
    If Trk = Nil Then Exit;
    Trk.Layer := PcbLayerOf(FldI(line, 2));
    Trk.Width := FldI(line, 3);
    Trk.X1 := FldI(line, 4);
    Trk.Y1 := FldI(line, 5);
    Trk.X2 := FldI(line, 6);
    Trk.Y2 := FldI(line, 7);
    Comp.AddPCBObject(Trk);
End;

Procedure AddArc(Comp, Lib, line);
Var
    Arc;
Begin
    UseLayer(Lib, FldI(line, 2));
    Arc := PCBServer.PCBObjectFactory(eArcObject, eNoDimension, eCreate_Default);
    If Arc = Nil Then Exit;
    Arc.Layer := PcbLayerOf(FldI(line, 2));
    Arc.LineWidth := FldI(line, 3);
    Arc.XCenter := FldI(line, 4);
    Arc.YCenter := FldI(line, 5);
    Arc.Radius := FldI(line, 6);
    Arc.StartAngle := FldI(line, 7) / 10.0;
    Arc.EndAngle := FldI(line, 8) / 10.0;
    Comp.AddPCBObject(Arc);
End;

Procedure AddFpText(Comp, Lib, line);
Var
    Txt;
Begin
    UseLayer(Lib, FldI(line, 2));
    Txt := PCBServer.PCBObjectFactory(eTextObject, eNoDimension, eCreate_Default);
    If Txt = Nil Then Exit;
    Txt.Layer := PcbLayerOf(FldI(line, 2));
    Txt.XLocation := FldI(line, 3);
    Txt.YLocation := FldI(line, 4);
    Txt.Size := FldI(line, 5);
    Txt.Rotation := FldI(line, 6) / 10.0;
    Txt.Width := FldI(line, 7);
    Try
        Txt.MirrorFlag := (FldI(line, 8) = 1);
    Except
    End;
    Txt.Text := FldS(line, 9);
    Comp.AddPCBObject(Txt);
End;

Procedure AddFill(Comp, Lib, line);
Var
    Fl;
Begin
    UseLayer(Lib, FldI(line, 2));
    Fl := PCBServer.PCBObjectFactory(eFillObject, eNoDimension, eCreate_Default);
    If Fl = Nil Then Exit;
    Fl.Layer := PcbLayerOf(FldI(line, 2));
    Fl.X1Location := FldI(line, 3);
    Fl.Y1Location := FldI(line, 4);
    Fl.X2Location := FldI(line, 5);
    Fl.Y2Location := FldI(line, 6);
    Comp.AddPCBObject(Fl);
End;

Procedure AddBody(Comp, line);
Var
    Body;
    Mdl;
    path  : String;
Begin
    path := FldS(line, 2);
    If Not FileExists(path) Then
    Begin
        SayWarn('3D-модель не найдена: ' + path);
        Exit;
    End;
    Body := PCBServer.PCBObjectFactory(eComponentBodyObject, eNoDimension,
                                       eCreate_Default);
    If Body = Nil Then
    Begin
        SayWarn('не создалось 3D-тело для ' + ExtractFileName(path));
        Exit;
    End;
    Mdl := Nil;
    Try
        Mdl := Body.ModelFactory_FromFilename(path, False);
    Except
        Mdl := Nil;
    End;
    If Mdl = Nil Then
    Begin
        SayWarn('Altium не принял модель: ' + ExtractFileName(path));
        Exit;
    End;
    Try
        Body.SetState_FromModel;
    Except
    End;
    Try
        { поворот вокруг X/Y/Z и высота установки }
        Mdl.SetState(FldI(line, 6) / 10.0, FldI(line, 7) / 10.0,
                       FldI(line, 8) / 10.0, FldI(line, 5));
    Except
    End;
    Body.Model := Mdl;
    Try
        Body.StandoffHeight := FldI(line, 5);
    Except
    End;
    Comp.AddPCBObject(Body);
    If (FldI(line, 3) <> 0) Or (FldI(line, 4) <> 0) Then
        Try
            Body.MoveByXY(FldI(line, 3), FldI(line, 4));
        Except
        End;
    GNBody := GNBody + 1;
End;

{ Убрать из библиотеки посадку с таким именем (если она там уже есть). }
Procedure DropPcbComp(Lib, Name);
Var
    Iter;
    C;
    Victim;
Begin
    Victim := Nil;
    Try
        Iter := Lib.LibraryIterator_Create;
        Iter.SetState_FilterAll;
        C := Iter.FirstPCBObject;
        While C <> Nil Do
        Begin
            If UpperCase(C.Name) = UpperCase(Name) Then Victim := C;
            C := Iter.NextPCBObject;
        End;
        Lib.LibraryIterator_Destroy(Iter);
    Except
        Victim := Nil;
    End;
    If Victim <> Nil Then
        Try
            Lib.RemoveComponent(Victim);
        Except
        End;
End;

Procedure BuildPcbLib;
Var
    SrvDoc;
    Lib;
    Comp;
    i         : Integer;
    line  : String;
    t     : String;
    FpOpen  : Boolean;
    nm    : String;
Begin
    If GPcbPath = '' Then Exit;

    Say('');
    Say('Библиотека посадочных мест: ' + GPcbPath);
    CloseIfOpen(GPcbPath);
    Backup(GPcbPath);
    If (GFresh = 1) And FileExists(GPcbPath) Then
        Try
            DeleteFile(GPcbPath);
        Except
        End;

    SrvDoc := Nil;
    Try
        If FileExists(GPcbPath) Then
            SrvDoc := Client.OpenDocument('PCBLIB', GPcbPath)
        Else
            SrvDoc := Client.OpenNewDocument('PCBLIB', GPcbPath, '', False);
    Except
        SrvDoc := Nil;
    End;
    If SrvDoc = Nil Then
    Begin
        SayErr('не удалось открыть/создать ' + GPcbPath);
        Exit;
    End;
    Client.ShowDocument(SrvDoc);

    If PCBServer = Nil Then
    Begin
        SayErr('PCB-сервер недоступен');
        Exit;
    End;
    Lib := PCBServer.GetCurrentPCBLibrary;
    If Lib = Nil Then
    Begin
        SayErr('не получить текущую библиотеку посадок');
        Exit;
    End;

    PCBServer.PreProcess;
    Try
        Comp := Nil;
        FpOpen := False;
        For i := 0 To GJob.Count - 1 Do
        Begin
            line := GJob[i];
            t := Tag(line);

            If t = 'FP' Then
            Begin
                nm := FldS(line, 2);
                If nm = '' Then Continue;
                DropPcbComp(Lib, nm);
                Comp := PCBServer.CreatePCBLibComp;
                If Comp = Nil Then
                Begin
                    SayErr('не создалась посадка ' + nm);
                    Continue;
                End;
                Comp.Name := nm;
                Try
                    Comp.Description := FldS(line, 3);
                Except
                End;
                Try
                    Comp.Height := FldI(line, 4);
                Except
                End;
                Lib.RegisterComponent(Comp);
                GNFp := GNFp + 1;
                If GMadePcb <> Nil Then GMadePcb.Add(nm);
                FpOpen := True;
                Say('  посадка: ' + nm);
            End
            Else If (t = 'ENDFP') And FpOpen Then
            Begin
                Comp := Nil;
                FpOpen := False;
            End
            Else If FpOpen And (Comp <> Nil) Then
            Begin
                If t = 'PAD' Then AddPad(Comp, line)
                Else If t = 'TRK' Then AddTrack(Comp, Lib, line)
                Else If t = 'FARC' Then AddArc(Comp, Lib, line)
                Else If t = 'FTXT' Then AddFpText(Comp, Lib, line)
                Else If t = 'FILL' Then AddFill(Comp, Lib, line)
                Else If t = 'BODY' Then AddBody(Comp, line);
            End;
        End;
    Finally
        PCBServer.PostProcess;
    End;

    { в свежесозданной библиотеке остаётся пустая заготовка PCBCOMPONENT_1 }
    If GNFp > 0 Then
    Begin
        DropPcbComp(Lib, 'PCBCOMPONENT_1');
        DropPcbComp(Lib, 'PCBComponent_1');
    End;

    Try
        Lib.Board.ViewManager_FullUpdate;
    Except
    End;
    Try
        SrvDoc.DoFileSave('PCBLIB');
        Say('  сохранена, посадок ' + IntToStr(GNFp) +
            ', площадок ' + IntToStr(GNPad) +
            ', 3D-моделей ' + IntToStr(GNBody));
    Except
        SayErr('не удалось сохранить ' + GPcbPath);
    End;

    { Перечитываем то, что получилось: в журнале должно быть видно, что
      в библиотеке лежит ровно то, что ожидалось. }
    Try
        Say('  в библиотеке посадок сейчас:');
        For i := 0 To Lib.ComponentCount - 1 Do
            Say('    ' + Lib.GetComponent(i).Name);
    Except
        SayWarn('не удалось перечитать библиотеку посадок');
    End;
    Try
        Client.CloseDocument(SrvDoc);
    Except
    End;
End;

{ ====================================================== сборка .SchLib ==== }

Function FontIdFor(Size : Integer; Bold : Boolean) : Integer;
Begin
    Result := 0;
    Try
        Result := SchServer.FontManager.GetFontID(Size, 0, False, False,
                                                  Bold, False, GFont);
    Except
        Result := 0;
    End;
End;

Procedure AddPin(Comp, line);
Var
    Pin;
    fl        : Integer;
    part      : Integer;
Begin
    Pin := SchServer.SchObjectFactory(ePin, eCreate_Default);
    If Pin = Nil Then Exit;
    part := FldI(line, 2);
    If part < 1 Then part := 1;
    Pin.Designator := FldS(line, 3);
    Pin.Name := FldS(line, 4);
    Try
        Pin.Electrical := FldI(line, 5);
    Except
    End;
    { Поля 6,7 -- конец вывода у корпуса; 11,12 -- электрический конец.
      Что из этого Altium считает Location, задаёт строка PINLOC. }
    If (GPinLoc = 1) And (Fld(line, 11) <> '') Then
        Pin.Location := Point(FldI(line, 11), FldI(line, 12))
    Else
        Pin.Location := Point(FldI(line, 6), FldI(line, 7));
    Pin.PinLength := FldI(line, 8);
    Case FldI(line, 9) Of
        90  : Pin.Orientation := eRotate90;
        180 : Pin.Orientation := eRotate180;
        270 : Pin.Orientation := eRotate270;
    Else
        Pin.Orientation := eRotate0;
    End;
    fl := FldI(line, 10);
    Pin.ShowName := (fl And 1) <> 0;
    Pin.ShowDesignator := (fl And 2) <> 0;
    Try
        Pin.IsHidden := (fl And 4) <> 0;
    Except
    End;
    Try
        Pin.Color := GColP;
    Except
    End;
    Pin.OwnerPartId := part;
    Try
        Pin.OwnerPartDisplayMode := Comp.DisplayMode;
    Except
    End;
    Comp.AddSchObject(Pin);
    GNPin := GNPin + 1;
End;

Procedure AddSchLine(Comp, line);
Var
    Ln;
Begin
    Ln := SchServer.SchObjectFactory(eLine, eCreate_Default);
    If Ln = Nil Then Exit;
    Ln.OwnerPartId := FldI(line, 2);
    Ln.LineWidth := FldI(line, 3);
    Ln.Color := FldCol(line, 8, GColG);
    Ln.Location := Point(FldI(line, 4), FldI(line, 5));
    Ln.Corner := Point(FldI(line, 6), FldI(line, 7));
    Comp.AddSchObject(Ln);
End;

Procedure AddSchRect(Comp, line);
Var
    R;
Begin
    R := SchServer.SchObjectFactory(eRectangle, eCreate_Default);
    If R = Nil Then Exit;
    R.OwnerPartId := FldI(line, 2);
    R.LineWidth := FldI(line, 3);
    R.Color := FldCol(line, 9, GColG);
    R.Location := Point(FldI(line, 4), FldI(line, 5));
    R.Corner := Point(FldI(line, 6), FldI(line, 7));
    Try
        R.IsSolid := (FldI(line, 8) = 1);
        R.Transparent := (FldI(line, 8) <> 1);
    Except
    End;
    Comp.AddSchObject(R);
End;

Procedure AddSchArc(Comp, line);
Var
    A;
Begin
    A := SchServer.SchObjectFactory(eArc, eCreate_Default);
    If A = Nil Then Exit;
    A.OwnerPartId := FldI(line, 2);
    A.LineWidth := FldI(line, 3);
    A.Color := GColG;
    A.Location := Point(FldI(line, 4), FldI(line, 5));
    A.Radius := FldI(line, 6);
    A.StartAngle := FldI(line, 7) / 10.0;
    A.EndAngle := FldI(line, 8) / 10.0;
    Comp.AddSchObject(A);
End;

Procedure AddSchEllipse(Comp, line);
Var
    E;
Begin
    E := SchServer.SchObjectFactory(eEllipse, eCreate_Default);
    If E = Nil Then Exit;
    E.OwnerPartId := FldI(line, 2);
    E.LineWidth := FldI(line, 3);
    E.Color := GColG;
    E.Location := Point(FldI(line, 4), FldI(line, 5));
    E.Radius := FldI(line, 6);
    Try
        E.IsSolid := (FldI(line, 7) = 1);
    Except
    End;
    Comp.AddSchObject(E);
End;

Procedure AddSchPoly(Comp, line);
Var
    Ln;
    n, k      : Integer;
    x1, y1    : Integer;
    x2, y2    : Integer;
Begin
    { Ломаную выкладываем отрезками: ISch_Polygon требует Vertex[], а этого
      свойства в рабочих скриптах никто не трогает -- значит и мы не будем.
      Теряется только заливка. }
    n := FldI(line, 5);
    If n < 2 Then Exit;
    For k := 1 To n - 1 Do
    Begin
        x1 := FldI(line, 4 + k * 2);
        y1 := FldI(line, 5 + k * 2);
        x2 := FldI(line, 6 + k * 2);
        y2 := FldI(line, 7 + k * 2);
        Ln := SchServer.SchObjectFactory(eLine, eCreate_Default);
        If Ln <> Nil Then
        Begin
            Ln.OwnerPartId := FldI(line, 2);
            Ln.LineWidth := FldI(line, 3);
            Ln.Color := GColG;
            Ln.Location := Point(x1, y1);
            Ln.Corner := Point(x2, y2);
            Comp.AddSchObject(Ln);
        End;
    End;
End;

Procedure AddSchText(Comp, line);
Var
    L;
Begin
    L := SchServer.SchObjectFactory(eLabel, eCreate_Default);
    If L = Nil Then Exit;
    L.OwnerPartId := FldI(line, 2);
    L.Location := Point(FldI(line, 3), FldI(line, 4));
    L.FontId := FontIdFor(FldI(line, 5), FldI(line, 8) = 1);
    L.Color := FldI(line, 10);
    Case FldI(line, 6) Of
        90  : L.Orientation := eRotate90;
        180 : L.Orientation := eRotate180;
        270 : L.Orientation := eRotate270;
    Else
        L.Orientation := eRotate0;
    End;
    Try
        L.Justification := FldI(line, 7);
    Except
    End;
    L.Text := FldS(line, 9);
    Comp.AddSchObject(L);
End;

Procedure AddSchParam(Comp, line, X, Y);
Var
    P;
Begin
    P := SchServer.SchObjectFactory(eParameter, eCreate_Default);
    If P = Nil Then Exit;
    P.OwnerPartId := -1;
    P.Name := FldS(line, 2);
    P.Text := FldS(line, 3);
    P.ShowName := False;
    P.Color := GColT;
    P.IsHidden := (FldI(line, 4) = 1);
    P.Location := Point(X, Y);
    Try
        P.FontId := FontIdFor(8, False);
    Except
    End;
    Comp.AddSchObject(P);
End;

Procedure AddSchFootprint(Comp, line);
Var
    Impl;
    nm   : String;
Begin
    nm := FldS(line, 2);
    If nm = '' Then Exit;
    Try
        Impl := SchServer.SchObjectFactory(eImplementation, eCreate_Default);
    Except
        Impl := Nil;
    End;
    If Impl = Nil Then
    Begin
        SayWarn('не привязалось посадочное место ' + nm +
                ' (привяжите вручную в редакторе символа)');
        Exit;
    End;
    Try
        Impl.ModelName := nm;
        Impl.ModelType := 'PCBLIB';
        Impl.Description := '';
        { OwnerPartId у ISch_Implementation нет -- не трогаем }
        Impl.IsCurrent := True;
        Comp.AddSchObject(Impl);
    Except
        SayWarn('не привязалось посадочное место ' + nm);
    End;
End;

Procedure DropSchComp(Lib, Name);
Var
    Iter;
    C;
    V;
Begin
    V := Nil;
    Try
        Iter := Lib.SchLibIterator_Create;
        Iter.AddFilter_ObjectSet(MkSet(eSchComponent));
        C := Iter.FirstSchObject;
        While C <> Nil Do
        Begin
            If UpperCase(C.LibReference) = UpperCase(Name) Then V := C;
            C := Iter.NextSchObject;
        End;
        Lib.SchIterator_Destroy(Iter);
    Except
        V := Nil;
    End;
    If V <> Nil Then
        Try
            Lib.RemoveSchComponent(V);
        Except
        End;
End;

Procedure BuildSchLib;
Var
    SrvDoc;
    Lib;
    Comp;
    Iter;
    C;
    i         : Integer;
    line   : String;
    t      : String;
    SymOpen    : Boolean;
    nm     : String;
    dX, dY    : Integer;
    pY        : Integer;
    parts     : Integer;
Begin
    If GSchPath = '' Then Exit;

    Say('');
    Say('Схемная библиотека: ' + GSchPath);
    CloseIfOpen(GSchPath);
    Backup(GSchPath);
    If (GFresh = 1) And FileExists(GSchPath) Then
        Try
            DeleteFile(GSchPath);
        Except
        End;

    SrvDoc := Nil;
    Try
        If FileExists(GSchPath) Then
            SrvDoc := Client.OpenDocument('SCHLIB', GSchPath)
        Else
            SrvDoc := Client.OpenNewDocument('SCHLIB', GSchPath, '', False);
    Except
        SrvDoc := Nil;
    End;
    If SrvDoc = Nil Then
    Begin
        SayErr('не удалось открыть/создать ' + GSchPath);
        Exit;
    End;
    Client.ShowDocument(SrvDoc);

    If SchServer = Nil Then
    Begin
        SayErr('SCH-сервер недоступен');
        Exit;
    End;
    Lib := SchServer.GetCurrentSchDocument;
    If Lib = Nil Then
    Begin
        SayErr('не получить текущую схемную библиотеку');
        Exit;
    End;

    Comp := Nil;
    SymOpen := False;
    dX := 0; dY := 0; pY := 0;

    For i := 0 To GJob.Count - 1 Do
    Begin
        line := GJob[i];
        t := Tag(line);

        If t = 'COMP' Then
        Begin
            nm := FldS(line, 2);
            If nm = '' Then Continue;
            DropSchComp(Lib, nm);
            Comp := SchServer.SchObjectFactory(eSchComponent, eCreate_Default);
            If Comp = Nil Then
            Begin
                SayErr('не создался символ ' + nm);
                Continue;
            End;
            Comp.LibReference := nm;
            Try
                Comp.ComponentDescription := FldS(line, 4);
            Except
            End;
            Comp.CurrentPartID := 1;
            parts := FldI(line, 6);
            If parts > 1 Then
                Try
                    Comp.PartCount := parts;
                Except
                End;
            Try
                Comp.Designator.Text := FldS(line, 3);
                Comp.Designator.IsHidden := False;
            Except
            End;
            Try
                Comp.Comment.Text := FldS(line, 5);
                { тип уже нарисован своим текстом шрифтом ГОСТ -- штатный
                  комментарий дал бы вторую такую же надпись }
                Comp.Comment.IsHidden := (GHideCmt = 1);
            Except
            End;
            dX := 0; dY := 1000000; pY := 0;
            SymOpen := True;
            Say('  символ: ' + nm);
        End
        Else If (t = 'ENDCOMP') And SymOpen Then
        Begin
            If Comp <> Nil Then
            Begin
                Lib.AddSchComponent(Comp);
                Try
                    SchServer.RobotManager.SendMessage(Lib.I_ObjectAddress,
                        c_BroadCast, SCHM_PrimitiveRegistration,
                        Comp.I_ObjectAddress);
                Except
                End;
                GNComp := GNComp + 1;
                If GMadeSch <> Nil Then GMadeSch.Add(nm);
            End;
            Comp := Nil;
            SymOpen := False;
        End
        Else If SymOpen And (Comp <> Nil) Then
        Begin
            If t = 'DESIG' Then
            Begin
                dX := FldI(line, 2);
                dY := FldI(line, 3);
                pY := dY;
                Try
                    Comp.Designator.Location := Point(dX, dY);
                    Comp.Designator.FontId := FontIdFor(10, False);
                    Comp.Designator.Color := GColT;
                Except
                End;
            End
            Else If t = 'CMTPOS' Then
                Try
                    Comp.Comment.Location := Point(FldI(line, 2), FldI(line, 3));
                    Comp.Comment.FontId := FontIdFor(10, False);
                    Comp.Comment.Color := GColT;
                Except
                End
            Else If t = 'PARAM' Then
            Begin
                pY := pY - 100000;
                AddSchParam(Comp, line, dX, pY);
            End
            Else If t = 'FPREF' Then AddSchFootprint(Comp, line)
            Else If t = 'PIN' Then AddPin(Comp, line)
            Else If t = 'SLINE' Then AddSchLine(Comp, line)
            Else If t = 'SRECT' Then AddSchRect(Comp, line)
            Else If t = 'SARC' Then AddSchArc(Comp, line)
            Else If t = 'SELL' Then AddSchEllipse(Comp, line)
            Else If t = 'SPOLY' Then AddSchPoly(Comp, line)
            Else If t = 'STEXT' Then AddSchText(Comp, line);
        End;
    End;

    Try
        Lib.GraphicallyInvalidate;
    Except
    End;
    Try
        SrvDoc.DoFileSave('SCHLIB');
        Say('  сохранена, символов ' + IntToStr(GNComp) +
            ', выводов ' + IntToStr(GNPin));
    Except
        SayErr('не удалось сохранить ' + GSchPath);
    End;

    Try
        Say('  в схемной библиотеке сейчас:');
        Iter := Lib.SchLibIterator_Create;
        Iter.AddFilter_ObjectSet(MkSet(eSchComponent));
        C := Iter.FirstSchObject;
        While C <> Nil Do
        Begin
            Say('    ' + C.LibReference);
            C := Iter.NextSchObject;
        End;
        Lib.SchIterator_Destroy(Iter);
    Except
        SayWarn('не удалось перечитать схемную библиотеку');
    End;

    { Заготовку Component_1 убираем последней и уже после сохранения:
      если этой сборке Altium метод не понравится, библиотека всё равно
      уже лежит на диске. }
    If GNComp > 0 Then
    Begin
        DropSchComp(Lib, 'Component_1');
        Try
            SrvDoc.DoFileSave('SCHLIB');
        Except
        End;
    End;
    Try
        Client.CloseDocument(SrvDoc);
    Except
    End;
End;

{ ================================================= интегрированная ======== }

{ Собрать .IntLib из .LibPkg, который написал GostLib.

  Altium компилирует пакет библиотек сам, поэтому нам нужно только открыть
  проект и запустить компиляцию. Пока .SchLib/.PcbLib подключены как файлы,
  компилятор ругается на дубли имён -- поэтому сначала отключаем их. }
Procedure BuildIntLib;
Var
    WS;
    Prj;
    dir  : String;
Begin
    GIntPath := '';
    If GLibPkg = '' Then Exit;
    If Not FileExists(GLibPkg) Then
    Begin
        SayWarn('не найден пакет библиотек: ' + GLibPkg);
        Exit;
    End;

    Say('');
    Say('Интегрированная библиотека из ' + ExtractFileName(GLibPkg));

    { файловые библиотеки не должны быть подключены -- иначе дубли }
    UninstallLib(GSchPath);
    UninstallLib(GPcbPath);

    WS := Nil;
    Try
        WS := GetWorkspace;
    Except
        WS := Nil;
    End;
    If WS = Nil Then
    Begin
        SayWarn('рабочее пространство недоступно, .IntLib не собран');
        Exit;
    End;

    Prj := Nil;
    Try
        Prj := WS.DM_OpenProject(GLibPkg, True);
    Except
        Prj := Nil;
    End;
    If Prj = Nil Then
    Begin
        SayWarn('не удалось открыть ' + GLibPkg);
        Exit;
    End;

    Try
        ResetParameters;
        AddStringParameter('Action', 'Compile');
        AddStringParameter('ObjectKind', 'Project');
        RunProcess('WorkspaceManager:Compile');
    Except
        SayWarn('компиляция пакета не запустилась');
        Exit;
    End;

    dir := ExtractFilePath(GLibPkg);
    GIntPath := dir + 'Project Outputs for ' +
                ChangeFileExt(ExtractFileName(GLibPkg), '') + '\\' +
                ChangeFileExt(ExtractFileName(GLibPkg), '.IntLib');
    If Not FileExists(GIntPath) Then
        GIntPath := ChangeFileExt(GLibPkg, '.IntLib');
    If FileExists(GIntPath) Then
        Say('  собрана: ' + GIntPath)
    Else
    Begin
        SayWarn('.IntLib не найден после компиляции -- смотрите панель ' +
                'Messages в Altium');
        GIntPath := '';
    End;
End;

{ =============================================== старый режим доставки ==== }

Procedure LegacyDeploy;
Var
    i         : Integer;
    line : String;
    p    : String;
Begin
    Say('Задание старого формата -- только подключение библиотек.');
    For i := 0 To GJob.Count - 1 Do
    Begin
        line := GJob[i];
        If Tag(line) = 'LIB' Then
        Begin
            p := Trim(Fld(line, 2));
            If p = '' Then Continue;
            If FileExists(p + '.new') Then
            Begin
                CloseIfOpen(p);
                Try
                    If FileExists(p) Then DeleteFile(p);
                    RenameFile(p + '.new', p);
                Except
                End;
            End;
            CloseIfOpen(p);
            InstallLib(p);
        End;
    End;
End;

{ ============================================================ главный ===== }

Procedure ReadHeader;
Var
    i         : Integer;
    line : String;
    t    : String;
Begin
    GSchPath := '';
    GPcbPath := '';
    GFont := 'GOST type B';
    GInstall := 1;
    GFresh := 1;
    GPinLoc := 0;
    GIntLib := 0;
    GColG := 0;
    GColT := 0;
    GColP := 0;
    GHideCmt := 0;
    GLibPkg := '';
    For i := 0 To GJob.Count - 1 Do
    Begin
        line := GJob[i];
        t := Tag(line);
        If t = 'SCHLIB' Then GSchPath := FldS(line, 2)
        Else If t = 'PCBLIB' Then GPcbPath := FldS(line, 2)
        Else If t = 'FONT' Then GFont := FldS(line, 2)
        Else If t = 'INSTALL' Then GInstall := FldI(line, 2)
        Else If t = 'FRESH' Then GFresh := FldI(line, 2)
        Else If t = 'PINLOC' Then GPinLoc := FldI(line, 2)
        Else If t = 'INTLIB' Then GIntLib := FldI(line, 2)
        Else If t = 'COLGRAPH' Then GColG := FldI(line, 2)
        Else If t = 'COLTEXT' Then GColT := FldI(line, 2)
        Else If t = 'COLPIN' Then GColP := FldI(line, 2)
        Else If t = 'HIDECMT' Then GHideCmt := FldI(line, 2)
        Else If t = 'LIBPKG' Then GLibPkg := FldS(line, 2)
        Else If t = 'LOG' Then GLogPath := FldS(line, 2)
        Else If t = 'VENDOR' Then GVendor.Add(FldS(line, 2))
        Else If t = 'FP' Then Break
        Else If t = 'COMP' Then Break;
    End;
End;

{ Отчёт для утилиты: что реально оказалось в библиотеках.

  Журнал человек читает глазами, а программе нужен разбираемый список --
  иначе «собрал, а половина не обновилась» не отличить от «показалось».
  Имена копим прямо во время сборки: перечитывать библиотеку итератором
  ненадёжно (документ может быть ещё не сохранён), а так список ровно
  тот, что скрипт создал. }
Procedure WriteResult(JobPath : String);
Var
    R : TStringList;
    i : Integer;
Begin
    R := TStringList.Create;
    Try
        R.Add('VER' + #9 + '1');
        R.Add('JOB' + #9 + JobPath);
        R.Add('SCHLIB' + #9 + GSchPath);
        R.Add('PCBLIB' + #9 + GPcbPath);
        If GMadeSch <> Nil Then
            For i := 0 To GMadeSch.Count - 1 Do
                R.Add('SCH' + #9 + GMadeSch[i]);
        If GMadePcb <> Nil Then
            For i := 0 To GMadePcb.Count - 1 Do
                R.Add('PCB' + #9 + GMadePcb[i]);
        R.Add('NSCH' + #9 + IntToStr(GNComp));
        R.Add('NPCB' + #9 + IntToStr(GNFp));
        R.Add('NPAD' + #9 + IntToStr(GNPad));
        R.Add('NBODY' + #9 + IntToStr(GNBody));
        R.Add('WARN' + #9 + IntToStr(GWarn));
        R.Add('ERR' + #9 + IntToStr(GErr));
        R.Add('END' + #9 + '1');
        Try
            R.SaveToFile(JobPath + '.done');
        Except
        End;
    Finally
        R.Free;
    End;
End;

Procedure RunGostLib;
Var
    JobPath : String;
    ver       : Integer;
    i         : Integer;
    msg     : String;
Begin
    GErr := 0; GWarn := 0;
    GNComp := 0; GNFp := 0; GNPad := 0; GNBody := 0; GNPin := 0;

    JobPath := FindJobFile;
    If (JobPath = '') Or (Not FileExists(JobPath)) Then
    Begin
        ShowMessage('Задание не найдено.' + #13#10 +
                    'Указатель: ' + PointerPath + #13#10 +
                    'Запустите GostLib и нажмите «Собрать в Altium».');
        Exit;
    End;

    GLog := TStringList.Create;
    GJob := TStringList.Create;
    GVendor := TStringList.Create;
    GMadeSch := TStringList.Create;
    GMadePcb := TStringList.Create;
    GLogPath := JobPath + '.log';
    Try
        Try
            GJob.LoadFromFile(JobPath);
        Except
            ShowMessage('Не удалось прочитать ' + JobPath);
            Exit;
        End;

        Say('GostLibBuilder v3');
        Say('Задание: ' + JobPath);
        Say('Строк в задании: ' + IntToStr(GJob.Count));

        ver := 0;
        If GJob.Count > 0 Then
            If Tag(GJob[0]) = 'VER' Then ver := FldI(GJob[0], 2);

        If ver < JOB_VERSION Then
        Begin
            LegacyDeploy;
        End
        Else
        Begin
            ReadHeader;
            Say('Схемная: ' + GSchPath);
            Say('Посадки: ' + GPcbPath);
            Say('Шрифт: ' + GFont);

            BuildPcbLib;
            BuildSchLib;

            If GIntLib = 1 Then BuildIntLib;

            If GInstall = 1 Then
            Begin
                Say('');
                Say('Подключение библиотек:');
                If GIntPath <> '' Then
                    { одна запись в панели Components вместо двух }
                    InstallLib(GIntPath)
                Else
                Begin
                    InstallLib(GPcbPath);
                    InstallLib(GSchPath);
                End;
                For i := 0 To GVendor.Count - 1 Do InstallLib(GVendor[i]);
                RefreshLibraries;
                Say('  список библиотек обновлён');
            End;
        End;

        WriteResult(JobPath);

        msg := 'GostLib: символов ' + IntToStr(GNComp) +
               ', посадок ' + IntToStr(GNFp) +
               ', площадок ' + IntToStr(GNPad) +
               ', 3D-моделей ' + IntToStr(GNBody) + '.';
        Say('');
        Say(msg);
        Say('Предупреждений: ' + IntToStr(GWarn) +
            ', ошибок: ' + IntToStr(GErr));
        FlushLog;

        ShowMessage(msg + #13#10 +
                    'Предупреждений ' + IntToStr(GWarn) +
                    ', ошибок ' + IntToStr(GErr) + '.' + #13#10 +
                    'Компоненты -- в панели Components.' + #13#10 +
                    'Журнал: ' + GLogPath);
    Finally
        FlushLog;
        GJob.Free;
        GVendor.Free;
        GMadeSch.Free;
        GMadePcb.Free;
        GLog.Free;
        GLog := Nil;
        GJob := Nil;
        GVendor := Nil;
        GMadeSch := Nil;
        GMadePcb := Nil;
    End;
End;

{ ========================================================= диагностика ==== }

Procedure GostLibWhereIsJob;
Begin
    ShowMessage('Файл-указатель: ' + PointerPath + #13#10 +
                'Текущее задание: ' + FindJobFile);
End;

Procedure GostLibCheckFile;
Var
    L : TStringList;
    S : String;
    i         : Integer;
    p : String;
Begin
    p := FindJobFile;
    If (p = '') Or (Not FileExists(p)) Then
    Begin
        ShowMessage('Задание не найдено: ' + PointerPath);
        Exit;
    End;
    L := TStringList.Create;
    Try
        L.LoadFromFile(p);
        S := 'Задание: ' + p + #13#10 +
             'Строк: ' + IntToStr(L.Count) + #13#10 + #13#10;
        For i := 0 To L.Count - 1 Do
        Begin
            If i > 14 Then Break;
            S := S + Unesc(L[i]) + #13#10;
        End;
        S := S + #13#10 + 'Проверка кириллицы: ' + Unesc('\u041F\u0440\u043E\u0432\u0435\u0440\u043A\u0430');
        ShowMessage(S);
    Finally
        L.Free;
    End;
End;

Procedure GostLibListInstalled;
Var
    i         : Integer;
    S : String;
Begin
    S := '';
    If IntegratedLibraryManager = Nil Then
    Begin
        ShowMessage('Менеджер библиотек недоступен.');
        Exit;
    End;
    For i := 0 To IntegratedLibraryManager.AvailableLibraryCount - 1 Do
        S := S + IntegratedLibraryManager.InstalledLibraryPath(i) + #13#10;
    ShowMessage('Подключённые библиотеки:' + #13#10 + S);
End;

End.
