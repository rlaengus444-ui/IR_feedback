// Synthetic WinForms fixture only: no BinderJet, hardware, or external settings.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;
[assembly:System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8",FrameworkDisplayName=".NET Framework 4.8")]
internal static class SettingsRecoveryFixture {
    [DllImport("user32.dll")] private static extern IntPtr SetParent(IntPtr child, IntPtr parent);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr window);
    private sealed class PoisonPanel : Panel {
        public string Marker;
        protected override void WndProc(ref Message m) {
            if (m.Msg == 0x003D || m.Msg == 0x000D || m.Msg == 0x0114 || m.Msg == 0x0115)
                File.AppendAllText(Marker, m.Msg.ToString("X") + "\n");
            base.WndProc(ref m);
        }
    }
    [STAThread] private static void Main(string[] args) {
        string marker = args[0];
        Application.EnableVisualStyles();
        var root = new Form { Text="Synthetic recovery fixture", Width=700, Height=500, ShowInTaskbar=false };
        var machine = new Label { Name="labelMachineName", Text="M1", Left=5, Top=5 };
        var state = new Label { Name="labelTaskStatus", Text="AUTO", Left=110, Top=5 };
        var nav = new Button { Name="btnSequences", Text="Sequences", Left=220, Top=5 };
        int saves=0, navs=0;
        nav.Click += delegate { navs++; };
        root.Controls.Add(machine); root.Controls.Add(state); root.Controls.Add(nav);
        var outside = new Form();
        Panel panel = null; TextBox ir = null, other = null; Button save = null;
        Action<bool> makePanel = delegate(bool duplicate) {
            panel = new Panel { Name="SequenceView", Left=10, Top=45, Width=400, Height=200 };
            ir = new TextBox { Name="textBoxHeatingOutput", Text="62", Left=5, Top=5 };
            other = new TextBox { Name="textBoxSpeed", Text="5", Left=130, Top=5 };
            save = new Button { Name="btnSave", Text="Save", Left=5, Top=40 };
            save.Click += delegate { saves++; };
            panel.Controls.Add(ir); panel.Controls.Add(other); panel.Controls.Add(save);
            if (duplicate) panel.Controls.Add(new TextBox { Name="textBoxHeatingOutput", Text="62", Left=240, Top=5 });
            root.Controls.Add(panel);
            foreach (Control c in panel.Controls) { var unused=c.Handle; }
        };
        makePanel(false);
        var poison = new PoisonPanel { Name="SyntheticProcessGrid", Marker=marker+".poison", Left=500, Top=300, Width=100, Height=100 };
        root.Controls.Add(poison); var poisonHandle=poison.Handle;
        int ticks=0; bool ready=false;
        var timer = new Timer { Interval=200 };
        timer.Tick += delegate {
            string command=marker+".command";
            if (File.Exists(command)) {
                string action=File.ReadAllText(command); File.Delete(command);
                if (action=="set") { ir.Text="67"; other.Text="9"; }
                else if (action=="pause") state.Text="PAUSED";
                else if (action=="auto") state.Text="AUTO";
                else if (action=="machine_s3") machine.Text="S3";
                else if (action=="machine_m1") machine.Text="M1";
                else if (action=="focus") { root.Activate(); SetForegroundWindow(root.Handle); ir.Focus(); }
                else if (action=="clear") { nav.Focus(); }
                else if (action=="missing") { var old=panel; var h=outside.Handle; SetParent(old.Handle,h); }
                else if (action=="recreate" || action=="duplicate") {
                    if (panel.Parent==root) { var old=panel; var h=outside.Handle; SetParent(old.Handle,h); }
                    makePanel(action=="duplicate");
                }
                File.WriteAllText(marker+".done",action);
            }
            File.WriteAllText(marker+".heartbeat", DateTime.UtcNow.Ticks+","+ir.Text+","+other.Text+","+saves+","+navs);
            if (!ready && ++ticks>=2) {
                if (File.Exists(marker+".poison")) File.Move(marker+".poison",marker+".startup-poison");
                File.WriteAllText(marker+".ready.tmp",Process.GetCurrentProcess().Id+","+root.Handle.ToInt64());
                File.Move(marker+".ready.tmp",marker+".ready"); ready=true;
            }
        };
        root.Shown += delegate { foreach(Control c in root.Controls) { var h=c.Handle; } nav.Focus(); timer.Start(); };
        Application.Run(root);
    }
}
