// Disposable synthetic process. It never opens a machine, edits IR, or saves.
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
using System.Runtime.InteropServices;
[assembly:System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8",FrameworkDisplayName=".NET Framework 4.8")]

internal static class NativeDiscoveryFixture {
    private sealed class PoisonLabel : Label {
        public string Marker;
        protected override void WndProc(ref Message m) {
            if (m.Msg == 0x003D || m.Msg == 0x000D)
                File.AppendAllText(Marker, "hidden-label=" + m.Msg.ToString("X") + "\n");
            base.WndProc(ref m);
        }
    }
    private sealed class PoisonPanel : Panel {
        public string Marker;
        protected override void WndProc(ref Message m) {
            if (m.Msg == 0x003D || m.Msg == 0x000D || m.Msg == 0x0114 || m.Msg == 0x0115)
                File.AppendAllText(Marker, m.Msg.ToString("X") + "\n");
            base.WndProc(ref m);
        }
    }
    [DllImport("user32.dll")] private static extern IntPtr SetParent(IntPtr child, IntPtr parent);
    [STAThread] private static void Main(string[] args) {
        string marker = args[0], mode = args[1];
        Application.EnableVisualStyles();
        var form = new Form { Text="Synthetic discovery fixture", Width=700, Height=500, ShowInTaskbar=false };
        var machine = new Label { Name="labelMachineName", Text="S3", Left=5, Top=5 };
        var state = new Label { Name="labelTaskStatus", Text="AUTO", Left=110, Top=5 };
        var nav = new Button { Name="btnSequences", Text="Sequences", Left=220, Top=5 };
        int navigationClicks=0;
        nav.Click += delegate { navigationClicks++; File.WriteAllText(marker+".nav-clicks", navigationClicks.ToString()); };
        form.Controls.Add(machine); form.Controls.Add(state); form.Controls.Add(nav);
        var sequence = new Panel { Name="SequenceView", Left=10, Top=40, Width=400, Height=200 };
        var ir = new TextBox { Name="textBoxHeatingOutput", Text="62", Left=5, Top=5 };
        var unrelated = new TextBox { Name="textBoxUnrelated", Text="5", Enabled=false, Left=120, Top=5 };
        var save = new Button { Name="btnSave", Text="Save", Left=5, Top=40 };
        sequence.Controls.Add(ir); sequence.Controls.Add(unrelated); sequence.Controls.Add(save); form.Controls.Add(sequence);
        int saves=0; save.Click += delegate { saves++; File.WriteAllText(marker+".saves", saves.ToString()); };
        if(mode=="duplicate_id") sequence.Controls.Add(new TextBox { Name="textBoxHeatingOutput", Text="62", Left=120 });
        if(mode=="conflict" || mode=="duplicate_nav") {
            for(int i=0;i<910;i++) form.Controls.Add(new Panel { Left=650, Top=450, Width=1, Height=1 });
        }
        if(mode=="conflict") form.Controls.Add(new Label { Text="M1", Left=5, Top=250 });
        if(mode=="duplicate_nav") form.Controls.Add(new Button { Text="Sequences", Left=110, Top=250 });
        if(mode=="bulk") {
            // Force handle creation even for invisible controls; poison unsupported panels.
            for(int i=0;i<1201;i++) { var p=new PoisonLabel { Marker=marker+".poison", Text="hidden", Visible=false, Width=1, Height=1 }; form.Controls.Add(p); var h=p.Handle; }
            for(int i=0;i<1201;i++) { var p=new PoisonPanel { Marker=marker+".poison", Left=650, Top=450, Width=1, Height=1 }; form.Controls.Add(p); var h=p.Handle; }
        }
        if(mode=="limit900" || mode=="limit901") {
            // Six metadata leaves precede fillers: machine, state, navigation, IR, disabled input, and Save.
            int count = mode=="limit900" ? 894 : 895;
            for(int i=0;i<count;i++) form.Controls.Add(new Label { Text="filler", Left=650, Top=450, Width=1, Height=1 });
            form.Controls.Add(new PoisonPanel { Marker=marker+".poison", Left=650, Top=450, Width=1, Height=1 });
        }
        var stale = new Label { Name="staleLabel", Text="fixture", Left=500, Top=10 };
        if(mode.StartsWith("stale_")) form.Controls.Add(stale);
        var reparentTarget = new Form();
        var heartbeat = new Timer { Interval=250 };
        heartbeat.Tick += delegate {
            string command = marker+".command";
            if (File.Exists(command)) {
                string action=File.ReadAllText(command); File.Delete(command);
                if(action=="hide") stale.Visible=false;
                else if(action=="destroy") stale.Dispose();
                else if(action=="reparent") { var target=reparentTarget.Handle; SetParent(stale.Handle,target); }
                File.WriteAllText(marker+".done", action);
            }
            File.WriteAllText(marker+".heartbeat", DateTime.UtcNow.Ticks+","+ir.Text+","+saves);
        };
        form.Shown += delegate {
            foreach(Control c in form.Controls) { var h=c.Handle; }
            if(mode.StartsWith("stale_")) File.WriteAllText(marker+".stale", stale.Handle.ToInt64().ToString());
            if (File.Exists(marker+".poison")) File.Delete(marker+".poison");
            File.WriteAllText(marker+".ready", Process.GetCurrentProcess().Id+","+form.Handle.ToInt64());
            File.WriteAllText(marker+".saves", "0"); heartbeat.Start();
        };
        Application.Run(form);
    }
}
