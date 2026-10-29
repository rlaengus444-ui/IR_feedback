// Entirely owned, synthetic WinForms UI. No BJT or physical equipment.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Windows.Forms;
[assembly:System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8",FrameworkDisplayName=".NET Framework 4.8")]
internal static class AutoSliceSourceFixture {
    private sealed class PoisonPanel : Panel {
        public string Marker;
        protected override void WndProc(ref Message m) {
            if(m.Msg==0x003D || m.Msg==0x000D || m.Msg==0x0114 || m.Msg==0x0115)
                File.AppendAllText(Marker,m.Msg.ToString("X")+"\n");
            base.WndProc(ref m);
        }
    }
    [STAThread] private static void Main(string[] args) {
        string marker=args[0];
        Application.EnableVisualStyles();
        var root=new Form {Text="Synthetic auto image source",Width=900,Height=580,
            TopMost=true,StartPosition=FormStartPosition.Manual,Location=new Point(40,40)};
        var machine=new Label {Name="labelMachineName",Text="M1",Left=5,Top=5};
        var state=new Label {Name="labelTaskStatus",Text="AUTO",Left=120,Top=5};
        var save=new Button {Name="btnSave",Text="Save",Left=240,Top=5};
        var nav=new Button {Name="btnSequences",Text="Sequences",Left=350,Top=5};
        int saves=0,navs=0;save.Click+=delegate{saves++;};nav.Click+=delegate{navs++;};
        root.Controls.AddRange(new Control[]{machine,state,save,nav});
        var home=new Form {Text="Home",TopLevel=false,FormBorderStyle=FormBorderStyle.None,
            Left=5,Top=50,Width=800,Height=240};
        root.Controls.Add(home);
        var production=new Panel {Name="ProductionControl",Dock=DockStyle.Fill};home.Controls.Add(production);
        TextBox path=null;Label label=null;
        Action makePath=delegate {
            path=new TextBox {Name="txtSlicingPath",Text=args[1],ReadOnly=true,Left=10,Top=20,Width=740};
            label=new Label {Name="lblSlicingPath",Text=args[1],Left=10,Top=65,Width=740,Height=25};
            production.Controls.Add(path);production.Controls.Add(label);
            var h=path.Handle;var l=label.Handle;
        };
        makePath();
        var process=new Form {Text="Process",TopLevel=false,FormBorderStyle=FormBorderStyle.None,
            Left=5,Top=310,Width=800,Height=180};root.Controls.Add(process);
        var poison=new PoisonPanel {Text="Synthetic grid",Marker=marker+".poison",Dock=DockStyle.Fill};
        process.Controls.Add(poison);
        var wrong=new Label {Text=args[1],Left=5,Top=5,Width=740};poison.Controls.Add(wrong);
        Action report=delegate {
            var box=path.PointToScreen(new Point(20,8));var lab=label.PointToScreen(new Point(20,8));
            File.WriteAllText(marker+".handles",Process.GetCurrentProcess().Id+","+root.Handle.ToInt64()+","+
                home.Handle.ToInt64()+","+path.Handle.ToInt64()+","+label.Handle.ToInt64()+","+
                machine.Handle.ToInt64()+","+wrong.Handle.ToInt64()+","+box.X+","+box.Y+","+lab.X+","+lab.Y);
        };
        var timer=new Timer{Interval=100};int ticks=0;
        timer.Tick+=delegate {
            string command=marker+".command";
            if(File.Exists(command)) {
                string action=File.ReadAllText(command);File.Delete(command);
                if(action=="b") {path.Text=args[2];label.Text=args[2];}
                else if(action=="a") {path.Text=args[1];label.Text=args[1];}
                else if(action=="hide")home.Hide();
                else if(action=="show")home.Show();
                else if(action=="recreate") {path.Dispose();label.Dispose();makePath();}
                report();File.WriteAllText(marker+".done",action);
            }
            File.WriteAllText(marker+".heartbeat",DateTime.UtcNow.Ticks+","+saves+","+navs);
            if(++ticks==3) {
                report();
                if(File.Exists(marker+".poison"))File.Move(marker+".poison",marker+".startup-poison");
                File.WriteAllText(marker+".ready","ready");
            }
        };
        root.Shown+=delegate {home.Show();process.Show();home.BringToFront();timer.Start();};
        Application.Run(root);
    }
}
