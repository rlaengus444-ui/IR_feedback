using System;
using System.IO;
using System.Globalization;
using System.Threading;
using System.Diagnostics;
using System.Windows.Forms;
using System.Runtime.InteropServices;
using System.Collections.Generic;
using System.Web.Script.Serialization;
[assembly:System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8",FrameworkDisplayName=".NET Framework 4.8")]
internal static class FullRouteFixture
{
    private sealed class QuietForm:Form { public bool AllowActivation;protected override bool ShowWithoutActivation { get { return !AllowActivation; } } }
    [DllImport("user32.dll")] private static extern bool SetWindowPos(IntPtr h,IntPtr after,int x,int y,int cx,int cy,uint flags);
    [STAThread] private static void Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        string marker=args[0], machine=args[1], mode=args.Length>2?args[2]:"success";
        Thread uiThread=Thread.CurrentThread;
        int heartbeatTicks=Environment.TickCount,watchdogStop=0;
        var watchdog=new Thread(delegate() {
            while(Volatile.Read(ref watchdogStop)==0) {
                Thread.Sleep(250);
                if(unchecked(Environment.TickCount-Volatile.Read(ref heartbeatTicks))<1500)continue;
                string stack;
                bool suspended=false;
                try {
#pragma warning disable 618
                    uiThread.Suspend();suspended=true;
                    Thread.Sleep(50);
                    stack=new StackTrace(uiThread,true).ToString();
#pragma warning restore 618
                } catch(Exception error) { stack=error.ToString(); }
                finally { if(suspended)try {
#pragma warning disable 618
                    uiThread.Resume();
#pragma warning restore 618
                } catch {} }
                File.WriteAllText(marker+".stack",stack);
                return;
            }
        }) { IsBackground=true };
        if(Array.IndexOf(args,"stack")>=0)watchdog.Start();
        var serializer=new JavaScriptSerializer();
        Dictionary<string,string> fieldSpec=args.Length>3?serializer.Deserialize<Dictionary<string,string>>(File.ReadAllText(args[3])):null;
        Application.ThreadException+=delegate(object sender,ThreadExceptionEventArgs error) { File.WriteAllText(marker+".error",error.Exception.ToString()); };
        string configs=Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"Machines",machine,"Configs");
        Directory.CreateDirectory(configs);
        string bjt=Path.Combine(configs,"BJT.parameters.json");
        string motion=Path.Combine(configs,"Motion.parameters.json");
        if(fieldSpec==null) {
            File.WriteAllText(bjt, Parameters("62"));
            File.WriteAllText(motion,"[{\"Scope\":\"Motion\",\"Name\":\"Speed\",\"Type\":\"DoubleParameter\",\"Value\":\"5\"}]");
        }
        File.WriteAllText(marker+".saves","0");
        File.WriteAllText(marker+".sequence-clicks","0");
        int saves=0,sequenceClicks=0;
        bool normallyVisible=Array.IndexOf(args,"visible")>=0;
        var form=new QuietForm { AllowActivation=Array.IndexOf(args,"activate")>=0,Name="FullRouteFixture",Text="BinderJet synthetic Sequence fixture",Left=200,Top=100,Width=760,Height=600,StartPosition=FormStartPosition.Manual,ShowInTaskbar=false,Opacity=normallyVisible?1:0.01 };
        form.Controls.Add(new Label { Name="labelMachineName",Text=machine,Top=10,Left=10,Width=100 });
        var taskStatus=new Label { Name="labelTaskStatus",Text="AUTO",Top=10,Left=150,Width=100 };
        form.Controls.Add(taskStatus);
        var processButton=new Button { Name="btnProcess",Text="Process",Top=40,Left=10 };
        var sequencesButton=new Button { Name="btnSequences",Text="Sequences",Top=40,Left=110 };
        form.Controls.Add(processButton); form.Controls.Add(sequencesButton);
        var process=new Panel { Name="ProcessPanel",Left=10,Top=90,Width=720,Height=440 };
        var sequence=new UserControl { Name="MeteorSequenceView",Left=10,Top=90,Width=720,Height=440,Visible=false };
        var webcam=new Panel { Name="WebCamPanel",Left=10,Top=90,Width=720,Height=440,Visible=mode=="bootstrap" };
        webcam.Controls.Add(new Label { Name="labelWebCam",Text="WebCam",Left=20,Top=20,Width=200 });
        form.Controls.Add(process);form.Controls.Add(sequence);
        form.Controls.Add(webcam);
        bool late=mode=="late"||mode=="latevisible";
        process.Controls.Add(new Label { Name="lblProgress",Text=late?"Layer 858/1000":"Layer 1/40",Top=10,Left=10,Width=200 });
        var grid=new DataGridView { Name="dataGridView1",Left=10,Top=50,Width=680,Height=300,ReadOnly=true,AllowUserToAddRows=false,VirtualMode=true };
        grid.RowTemplate.Height=104;
        grid.Columns.Add("Id","ID");grid.Columns.Add("TimeStart","Start");grid.Columns.Add("TempRecoating","T_Recoating");
        grid.Columns[1].Width=260;
        int cachedViewportFirstRow=0;
        grid.CellValueNeeded+=delegate(object sender,DataGridViewCellValueEventArgs cell) {
            bool cached=!late||Math.Abs(cell.RowIndex-cachedViewportFirstRow)<=128;
            if(cell.ColumnIndex==0)cell.Value=cell.RowIndex.ToString();
            if(cell.ColumnIndex==1)cell.Value=cached&&cell.RowIndex==(late?857:0)?"2026-10-27 08:30:00.123":"0001-01-01 00:00:00";
            if(cell.ColumnIndex==2)cell.Value=cached&&cell.RowIndex==(late?857:0)?"42":"0";
        };
        grid.RowCount=late?1000:40;
        process.Controls.Add(grid);
        var ir=new TextBox { Name="textBoxHeatingOutput",Text="62",Top=20,Left=20,Width=120 };
        var other=new TextBox { Name="textBoxOther",Text="5",Top=60,Left=20,Width=120 };
        var check=new CheckBox { Name="checkBoxOther",Text="Unrelated setting",Top=100,Left=20,Width=200 };
        var save=new Button { Name="btnSave",Text="SAVE",Top=150,Left=20,Width=100 };
        sequence.Controls.Add(ir);sequence.Controls.Add(other);sequence.Controls.Add(check);sequence.Controls.Add(save);
        if(fieldSpec!=null) {
            sequence.Controls.Remove(other);sequence.Controls.Remove(check);
            ir.Text=fieldSpec["textBoxHeatingOutput"];
            int number=0;
            foreach(var field in fieldSpec) {
                if(field.Key=="textBoxHeatingOutput")continue;
                int column=number/10,row=number%10;
                Control input;
                if(field.Value.StartsWith("toggle:")) input=new CheckBox { Name=field.Key,Checked=field.Value=="toggle:1",Text=field.Key,Left=160+column*180,Top=15+row*32,Width=170 };
                else input=new TextBox { Name=field.Key,Text=field.Value,Left=160+column*180,Top=15+row*32,Width=160 };
                if((mode=="dirty"||mode=="dirtysequence")&&number==0) {
                    var textInput=input as TextBox;if(textInput!=null)textInput.Text="9999";
                    var checkbox=input as CheckBox;if(checkbox!=null)checkbox.Checked=!checkbox.Checked;
                }
                sequence.Controls.Add(input);number++;
            }
        }
        ir.TextChanged+=delegate {
            if(mode=="edithang"&&ir.Text!="62") { File.WriteAllText(marker+".edited",ir.Text);Thread.Sleep(8000); }
            if(mode=="statechange"&&ir.Text!="62") { File.WriteAllText(marker+".edited",ir.Text);taskStatus.Text="IDLE"; }
        };
        processButton.Click+=delegate { File.AppendAllText(marker+".trace","process-start;");sequence.Visible=false;webcam.Visible=false;File.AppendAllText(marker+".trace","sequence-hidden;");process.Visible=true;File.AppendAllText(marker+".trace","process-shown;");process.BringToFront();File.AppendAllText(marker+".trace","process-end first="+grid.FirstDisplayedScrollingRowIndex+" displayed="+grid.DisplayedRowCount(true)+";"); };
        sequencesButton.Click+=delegate { sequenceClicks++;File.WriteAllText(marker+".sequence-clicks",sequenceClicks.ToString());process.Visible=false;webcam.Visible=false;sequence.Visible=true;sequence.BringToFront(); };
        if(mode=="bootstrap") { process.Visible=false;sequence.Visible=false;webcam.Visible=true;webcam.BringToFront(); }
        if(mode=="dirtysequence") { process.Visible=false;sequence.Visible=true;webcam.Visible=false;sequence.BringToFront(); }
        save.Click+=delegate {
            saves++;File.WriteAllText(marker+".saves",saves.ToString());
            if(mode=="savetardy")Thread.Sleep(4500);
            string temporary=bjt+".tmp";
            if(fieldSpec==null)File.WriteAllText(temporary,Parameters(ir.Text));
            else {
                var entries=(object[])serializer.DeserializeObject(File.ReadAllText(bjt));
                foreach(var raw in entries) {
                    var entry=(Dictionary<string,object>)raw;
                    if((string)entry["Scope"]=="Printing"&&(string)entry["Name"]=="IRHeatingOutputPct")entry["Value"]=ir.Text;
                }
                File.WriteAllText(temporary,serializer.Serialize(entries));
            }
            File.Replace(temporary,bjt,null);
            if(mode=="savehang")Thread.Sleep(8000);
        };
        var lifetime=new System.Windows.Forms.Timer { Interval=45000 };
        var heartbeat=new System.Windows.Forms.Timer { Interval=200 };
        heartbeat.Tick+=delegate { Volatile.Write(ref heartbeatTicks,Environment.TickCount);cachedViewportFirstRow=Math.Max(0,grid.FirstDisplayedScrollingRowIndex);File.WriteAllText(marker+".heartbeat",DateTime.UtcNow.ToString("O")+" row="+grid.FirstDisplayedScrollingRowIndex+" cell="+(grid.CurrentCell==null?-1:grid.CurrentCell.RowIndex)+" active="+(form.ActiveControl==null?"none":form.ActiveControl.Name)+" displayed="+grid.DisplayedRowCount(true));var state=new Dictionary<string,string>();foreach(Control control in sequence.Controls){var text=control as TextBox;if(text!=null)state[text.Name]=text.Text;var toggle=control as CheckBox;if(toggle!=null)state[toggle.Name]="toggle:"+(toggle.Checked?"1":"0");}File.WriteAllText(marker+".fields-state",serializer.Serialize(state)); };
        lifetime.Tick+=delegate { form.Close(); };
        form.Shown+=delegate { SetWindowPos(form.Handle,normallyVisible?IntPtr.Zero:new IntPtr(1),0,0,0,0,0x0001|0x0002|0x0010);if(mode=="latevisible"){grid.CurrentCell=grid.Rows[857].Cells[0];grid.FirstDisplayedScrollingRowIndex=857;}cachedViewportFirstRow=Math.Max(0,grid.FirstDisplayedScrollingRowIndex);if(form.AllowActivation)form.Activate();File.WriteAllText(marker+".initial-viewport",grid.FirstDisplayedScrollingRowIndex.ToString());File.WriteAllText(marker+".window",Process.GetCurrentProcess().Id+","+form.Handle.ToInt64());lifetime.Start();heartbeat.Start(); };
        Application.Run(form);
        Volatile.Write(ref watchdogStop,1);
    }
    private static string Parameters(string power) {
        double numeric=double.Parse(power,CultureInfo.InvariantCulture);
        return "[{\"Scope\":\"Printing\",\"Name\":\"IRHeatingOutputPct\",\"Type\":\"DoubleParameter\",\"Value\":\""+numeric.ToString(CultureInfo.InvariantCulture)+"\"},{\"Scope\":\"Printing\",\"Name\":\"Other\",\"Type\":\"DoubleParameter\",\"Value\":\"5\"}]";
    }
}
