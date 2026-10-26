using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class Launcher
{
    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string runtime = Path.Combine(root, "runtime", "pythonw.exe");
            string app = Path.Combine(root, "app", "standalone_ir.py");
            if (!File.Exists(runtime) || !File.Exists(app))
                throw new FileNotFoundException("ZIP 파일 전체를 압축 해제한 후 IRFeedback.exe를 실행하세요.");
            var start = new ProcessStartInfo(runtime, "-E -s -B \"" + app + "\"");
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            Process.Start(start);
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "IR 외부 모듈 실행 오류", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
