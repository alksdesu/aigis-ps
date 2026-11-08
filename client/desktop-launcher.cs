using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class DesktopLauncher
{
    [STAThread]
    private static int Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
        string directory = Path.Combine(root, "runtime", "desktop");
        try
        {
            if (!File.Exists(python))
                throw new FileNotFoundException("Run setup.ps1 -IncludeDesktop in the project directory first.", python);
            Directory.CreateDirectory(directory);
            Directory.CreateDirectory(Path.Combine(root, "tmp"));
            var options = new ProcessStartInfo(python, "-B -m aigis_ps.desktop")
            {
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            options.EnvironmentVariables["PYTHONPATH"] = Path.Combine(root, "src");
            options.EnvironmentVariables["PYTHONUTF8"] = "1";
            options.EnvironmentVariables["TEMP"] = Path.Combine(root, "tmp");
            options.EnvironmentVariables["TMP"] = Path.Combine(root, "tmp");
            using (var output = new StreamWriter(new FileStream(Path.Combine(directory, "launcher.log"),
                                                               FileMode.Append, FileAccess.Write, FileShare.ReadWrite)))
            using (var process = new Process { StartInfo = options })
            {
                output.AutoFlush = true;
                DataReceivedEventHandler receive = (sender, entry) =>
                {
                    if (entry.Data != null)
                        lock (output) output.WriteLine(entry.Data);
                };
                process.OutputDataReceived += receive;
                process.ErrorDataReceived += receive;
                process.Start();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                process.WaitForExit();
                if (process.ExitCode != 0)
                    MessageBox.Show("The console exited with an error. See runtime/desktop/launcher.log and desktop.log.",
                                    "Aigis Console", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return process.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Aigis Console", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
