using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;

namespace CodexCliStartup.Wrapper;

internal static class Program
{
    private const uint MessageBoxIconError = 0x00000010;

    [STAThread]
    private static int Main()
    {
        string executablePath = Environment.ProcessPath ?? AppContext.BaseDirectory;
        string launcherDirectory = Path.GetDirectoryName(executablePath) ?? AppContext.BaseDirectory;
        string launcherPath = Path.Combine(launcherDirectory, "codex-cli-startup.py");
        string venvPythonwPath = Path.Combine(launcherDirectory, ".venv", "Scripts", "pythonw.exe");
        string pythonwPath = File.Exists(venvPythonwPath) ? venvPythonwPath : "pythonw.exe";

        if (!File.Exists(launcherPath))
        {
            ShowError($"codex-cli-startup.py was not found in:{Environment.NewLine}{launcherDirectory}");
            return 1;
        }

        ProcessStartInfo startInfo = new ProcessStartInfo
        {
            FileName = pythonwPath,
            Arguments = QuoteArgument(launcherPath),
            WorkingDirectory = launcherDirectory,
            UseShellExecute = false,
        };

        try
        {
            Process.Start(startInfo);
        }
        catch (Win32Exception error)
        {
            ShowError($"Failed to start Python:{Environment.NewLine}{error.Message}");
            return 1;
        }

        return 0;
    }

    private static string QuoteArgument(string value)
    {
        return "\"" + value.Replace("\"", "\\\"", StringComparison.Ordinal) + "\"";
    }

    private static void ShowError(string message)
    {
        _ = MessageBox(IntPtr.Zero, message, "codex-cli-startup", MessageBoxIconError);
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBox(IntPtr windowHandle, string text, string caption, uint type);
}
