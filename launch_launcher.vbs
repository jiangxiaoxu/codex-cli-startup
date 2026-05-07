Option Explicit

Dim fileSystem
Dim shell
Dim scriptDir
Dim launcherPath
Dim venvPythonw
Dim pythonExe
Dim command

Set fileSystem = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fileSystem.GetParentFolderName(WScript.ScriptFullName)
launcherPath = fileSystem.BuildPath(scriptDir, "launcher.py")
venvPythonw = fileSystem.BuildPath(scriptDir, ".venv\Scripts\pythonw.exe")

If Not fileSystem.FileExists(launcherPath) Then
    MsgBox "launcher.py was not found in:" & vbCrLf & scriptDir, vbCritical, "Codex Workspace Launcher"
    WScript.Quit 1
End If

If fileSystem.FileExists(venvPythonw) Then
    pythonExe = venvPythonw
Else
    pythonExe = "pythonw.exe"
End If

shell.CurrentDirectory = scriptDir
command = Quote(pythonExe) & " " & Quote(launcherPath)
shell.Run command, 0, False

Function Quote(value)
    Quote = """" & value & """"
End Function
