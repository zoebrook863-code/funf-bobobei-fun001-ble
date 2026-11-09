' FUN001 automatic startup.
Set fs = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = ""
If Len(root) = 0 Then root = fs.GetParentFolderName(WScript.ScriptFullName)
script = fs.BuildPath(root, "start-fun001-component.ps1")
base = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & Chr(34) & script & Chr(34)
If WScript.Arguments.Named.Exists("check") Then
    WScript.Echo base
    WScript.Quit 0
End If
sh.Run base & " -Component control", 6
If fs.FileExists(fs.BuildPath(root, "private\http-mcp\.env")) Then
    sh.Run base & " -Component gateway", 0
End If
If fs.FileExists(fs.BuildPath(root, "private\tunnel.json")) Then
    sh.Run base & " -Component tunnel", 0
End If
