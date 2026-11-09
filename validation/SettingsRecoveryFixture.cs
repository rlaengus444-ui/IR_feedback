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
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr window, int command);
    [DllImport("user32.dll", EntryPoint="GetWindowLongW")] private static extern int GetWindowLong(IntPtr window, int index);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    private sealed class PoisonPanel : Panel {
        public string Marker;
        protected override void WndProc(ref Message m) {
            if (m.Msg == 0x003D || m.Msg == 0x000D || m.Msg == 0x0114 || m.Msg == 0x0115) {
                string stagePath = Marker.Substring(0, Marker.Length - ".poison".Length) + ".stage";
                string stage = "unlabelled";
                try { if (File.Exists(stagePath)) stage = File.ReadAllText(stagePath); } catch (IOException) { stage="stage-read-failed"; }
                File.AppendAllText(Marker, DateTime.UtcNow.ToString("o") + "|" + stage + "|" + m.Msg.ToString("X") + "\n");
            }
            base.WndProc(ref m);
        }
    }
    [STAThread] private static void Main(string[] args) {
        if (args.Length>0 && args[0]=="--foreground") {
            var foreground = new Form { Text="Independent foreground fixture", Width=250, Height=150 };
            foreground.Shown += delegate { foreground.Activate(); SetForegroundWindow(foreground.Handle); };
            Application.Run(foreground); return;
        }
        string marker = args[0];
        Application.EnableVisualStyles();
        bool full = args.Length>1;
        var root = new Form { Text="Synthetic recovery fixture", Width=full?1100:700, Height=full?850:500, ShowInTaskbar=false };
        var machine = new Label { Name="labelMachineName", Text=args.Length>2?args[2]:"M1", Left=5, Top=5 };
        var state = new Label { Name="labelTaskStatus", Text="AUTO", Left=110, Top=5 };
        var nav = new Button { Name="btnSequences", Text="Sequences", Left=220, Top=5 };
        int saves=0, navs=0;
        nav.Click += delegate { navs++; };
        root.Controls.Add(machine); root.Controls.Add(state); root.Controls.Add(nav);
        var outside = new Form();
        Panel panel = null, inner = null;
        var outer = new Panel { Name="SequenceOuter", Left=10, Top=45, Width=full?1000:400, Height=full?740:200 };
        root.Controls.Add(outer);
        TextBox ir = null, other = null, added = null; Button save = null;
        Action<bool> makePanel = delegate(bool duplicate) {
            panel = new Panel { Name="SequenceView", Left=0, Top=0, Width=full?1000:400, Height=full?740:200 };
            if(full) {
                int n=0; other=null;
                foreach(string line in File.ReadAllLines(args[1])) {
                    string[] parts=line.Split('\t');
                    int x=5+(n%4)*235, y=5+(n/4)*30;
                    if(parts[1]=="toggle") panel.Controls.Add(new CheckBox { Name=parts[0], Text=parts[0], Checked=false, Left=x, Top=y, Width=225 });
                    else {
                        var field=new TextBox { Name=parts[0], Text=parts[0]=="textBoxHeatingOutput"?"62":"0", Left=x, Top=y, Width=225 };
                        if (parts[0]=="textBoxLayerThickness") {
                            inner = new Panel { Name="InnerPanel", Left=x, Top=y, Width=225, Height=26 };
                            field.Left=0; field.Top=0; inner.Controls.Add(field); panel.Controls.Add(inner);
                        } else { panel.Controls.Add(field); }
                        if(parts[0]=="textBoxHeatingOutput") ir=field; else if(other==null) other=field;
                    }
                    n++;
                }
            } else {
                ir = new TextBox { Name="textBoxHeatingOutput", Text="62", Left=5, Top=5 };
                other = new TextBox { Name="textBoxSpeed", Text="5", Left=0, Top=0 };
                inner = new Panel { Name="InnerPanel", Left=130, Top=5, Width=100, Height=26 };
                inner.Controls.Add(other); panel.Controls.Add(ir); panel.Controls.Add(inner);
            }
            save = new Button { Name="btnSave", Text="Save", Left=5, Top=full?650:40 };
            save.Click += delegate { saves++; };
            panel.Controls.Add(save);
            if (duplicate) panel.Controls.Add(new TextBox { Name="textBoxHeatingOutput", Text="62", Left=240, Top=5 });
            outer.Controls.Add(panel);
            foreach (Control c in panel.Controls) { var unused=c.Handle; }
        };
        makePanel(false);
        Action writeStyles = delegate {
            string values = "outer="+GetWindowLong(outer.Handle,-16)+","+IsWindowVisible(outer.Handle)
                +"\nscope="+GetWindowLong(panel.Handle,-16)+","+IsWindowVisible(panel.Handle)
                +"\nir="+GetWindowLong(ir.Handle,-16)+","+IsWindowVisible(ir.Handle)
                +"\nsave="+GetWindowLong(save.Handle,-16)+","+IsWindowVisible(save.Handle)
                +"\ninner="+(inner==null ? "0,False" : GetWindowLong(inner.Handle,-16)+","+IsWindowVisible(inner.Handle));
            File.WriteAllText(marker+".styles", values);
        };
        var poison = new PoisonPanel { Name="SyntheticProcessGrid", Marker=marker+".poison", Left=full?1020:500, Top=full?750:300, Width=30, Height=30 };
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
                else if (action=="hide_outer") { ShowWindow(outer.Handle, 0); }
                else if (action=="show_outer") { ShowWindow(outer.Handle, 4); }
                else if (action=="hide_outer_managed") { outer.Visible=false; }
                else if (action=="show_outer_managed") { outer.Visible=true; }
                else if (action=="hide_ir") { ShowWindow(ir.Handle, 0); }
                else if (action=="show_ir") { ShowWindow(ir.Handle, 4); }
                else if (action=="hide_save") { ShowWindow(save.Handle, 0); }
                else if (action=="show_save") { ShowWindow(save.Handle, 4); }
                else if (action=="hide_inner") { ShowWindow(inner.Handle, 0); }
                else if (action=="show_inner") { ShowWindow(inner.Handle, 4); }
                else if (action=="minimize") { root.WindowState=FormWindowState.Minimized; }
                else if (action=="restore") { root.WindowState=FormWindowState.Normal; }
                else if (action=="blank_in_place") {
                    added = new TextBox { Name="", Text="1", Left=240, Top=5 };
                    panel.Controls.Add(added); var addedHandle=added.Handle;
                    ShowWindow(addedHandle, 4);
                }
                else if (action=="duplicate_in_place" || action=="extra_in_place") {
                    added = new TextBox { Name=action=="duplicate_in_place" ? "textBoxHeatingOutput" : "textBoxUnexpected", Text="1", Left=240, Top=5 };
                    panel.Controls.Add(added); var addedHandle=added.Handle;
                    ShowWindow(addedHandle, 4);
                }
                else if (action=="remove_extra") { if (added!=null) { added.Dispose(); added=null; } }
                else if (action=="missing") { var old=panel; var h=outside.Handle; SetParent(old.Handle,h); }
                else if (action=="recreate" || action=="duplicate") {
                    { var old=panel; var h=outside.Handle; SetParent(old.Handle,h); }
                    makePanel(action=="duplicate");
                }
                writeStyles();
                File.WriteAllText(marker+".done",action);
            }
            File.WriteAllText(marker+".heartbeat", DateTime.UtcNow.Ticks+","+ir.Text+","+other.Text+","+saves+","+navs);
            if (!ready && ++ticks>=2) {
                if (File.Exists(marker+".poison")) File.Move(marker+".poison",marker+".startup-poison");
                writeStyles();
                File.WriteAllText(marker+".ready.tmp",Process.GetCurrentProcess().Id+","+root.Handle.ToInt64());
                File.Move(marker+".ready.tmp",marker+".ready"); ready=true;
            }
        };
        root.Shown += delegate { foreach(Control c in root.Controls) { var h=c.Handle; } nav.Focus(); timer.Start(); };
        Application.Run(root);
    }
}
