using System;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using Accessibility;

// Native recipe inputs only; no accessibility trees or process memory APIs.
public static class BinderJetNativeFields
{
    private const uint Flags = 0x0001 | 0x0002 | 0x0020;
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll")] private static extern bool IsChild(IntPtr root, IntPtr child);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr window);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] private static extern bool IsWindowEnabled(IntPtr window);
    [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr window);
    [DllImport("user32.dll")] private static extern IntPtr GetParent(IntPtr window);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr window, StringBuilder value, int count);
    [DllImport("user32.dll", EntryPoint="GetWindowLongW")] private static extern int GetWindowLong(IntPtr window, int index);
    [DllImport("user32.dll", EntryPoint="SendMessageTimeoutW", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern IntPtr ReadMessage(IntPtr window, uint message, UIntPtr wParam, StringBuilder value, uint flags, uint timeout, out UIntPtr result);
    [DllImport("user32.dll", EntryPoint="SendMessageTimeoutW", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern IntPtr WriteMessage(IntPtr window, uint message, UIntPtr wParam, string value, uint flags, uint timeout, out UIntPtr result);
    [DllImport("oleacc.dll")] private static extern int AccessibleObjectFromWindow(IntPtr window, uint objectId,
        ref Guid interfaceId, [MarshalAs(UnmanagedType.Interface)] out object accessible);

    private static void Validate(IntPtr root, IntPtr child, int pid)
    {
        uint actual;
        if (pid<=0 || root==IntPtr.Zero || child==IntPtr.Zero || !IsWindow(root) || !IsWindow(child) ||
            GetWindowThreadProcessId(root,out actual)==0 || actual!=(uint)pid ||
            GetWindowThreadProcessId(child,out actual)==0 || actual!=(uint)pid ||
            (root!=child && !IsChild(root,child)))
            throw new InvalidOperationException("The recipe input/window identity changed.");
        if (!IsWindowVisible(root) || !IsWindowVisible(child) || IsIconic(root))
            throw new InvalidOperationException("The recipe input is not visible in its selected root.");
    }

    private static string Class(IntPtr window)
    {
        var value=new StringBuilder(256);
        if(GetClassName(window,value,value.Capacity)==0)throw new InvalidOperationException("The native input class is unavailable.");
        return value.ToString();
    }


    private static int Role(IntPtr child)
    {
        object client=null;
        try
        {
            Guid iid=new Guid("618736e0-3c3d-11cf-810c-00aa00389b71");
            if(AccessibleObjectFromWindow(child,unchecked((uint)-4),ref iid,out client)!=0 || !(client is IAccessible))
                throw new InvalidOperationException("The input has no public MSAA client.");
            return Convert.ToInt32(((IAccessible)client).get_accRole(0),CultureInfo.InvariantCulture);
        }
        finally { if(client!=null && Marshal.IsComObject(client))Marshal.ReleaseComObject(client); }
    }

    private static void EnabledChain(IntPtr root, IntPtr child)
    {
        IntPtr current=child;
        for(int depth=0;;depth++)
        {
            if(current==IntPtr.Zero || depth>64 || !IsWindowEnabled(current))
                throw new InvalidOperationException("The recipe input or parent is disabled/unverified.");
            if(current==root)return;
            current=GetParent(current);
        }
    }

    public static string Kind(IntPtr root, IntPtr child, int pid)
    {
        Validate(root,child,pid);
        string name=Class(child);
        if(name.IndexOf("EDIT",StringComparison.OrdinalIgnoreCase)>=0)return "text";
        return name.IndexOf("BUTTON",StringComparison.OrdinalIgnoreCase)>=0 && Role(child)==44 ? "toggle" : "";
    }

    public static void ValidateSave(IntPtr root, IntPtr button, int pid)
    {
        Validate(root,button,pid);
        EnabledChain(root,button);
        if(Class(button).IndexOf("BUTTON",StringComparison.OrdinalIgnoreCase)<0 || Role(button)!=43)
            throw new InvalidOperationException("Save is not a verified native PUSHBUTTON.");
    }

    public static string Read(IntPtr root, IntPtr child, int pid, string kind)
    {
        Validate(root,child,pid);
        if(kind=="text")
        {
            if(Class(child).IndexOf("EDIT",StringComparison.OrdinalIgnoreCase)<0)
                throw new InvalidOperationException("The recipe text input is not an EDIT window.");
            var value=new StringBuilder(4097);
            UIntPtr count;
            if(ReadMessage(child,0x000D,(UIntPtr)value.Capacity,value,Flags,250,out count)==IntPtr.Zero || count.ToUInt64()>=4096)
                throw new InvalidOperationException("The recipe text input did not answer within its bounded read.");
            Validate(root,child,pid);
            return value.ToString();
        }
        if(kind!="toggle")throw new ArgumentException("Only text or toggle inputs are supported.","kind");
        if(Class(child).IndexOf("BUTTON",StringComparison.OrdinalIgnoreCase)<0)
            throw new InvalidOperationException("The recipe checkbox is not a BUTTON window.");
        object client=null;
        try
        {
            Guid iid=new Guid("618736e0-3c3d-11cf-810c-00aa00389b71");
            if(AccessibleObjectFromWindow(child,unchecked((uint)-4),ref iid,out client)!=0 || !(client is IAccessible))
                throw new InvalidOperationException("The recipe checkbox has no public MSAA client.");
            var accessible=(IAccessible)client;
            if(Convert.ToInt32(accessible.get_accRole(0),CultureInfo.InvariantCulture)!=44)
                throw new InvalidOperationException("The recipe button is not the verified MSAA CHECKBUTTON role.");
            int state=Convert.ToInt32(accessible.get_accState(0),CultureInfo.InvariantCulture);
            Validate(root,child,pid);
            return (state&0x20)!=0 ? "toggle:2" : (state&0x10)!=0 ? "toggle:1" : "toggle:0";
        }
        finally { if(client!=null && Marshal.IsComObject(client))Marshal.ReleaseComObject(client); }
    }

    public static void SetIr(IntPtr root, IntPtr edit, int pid, string powerText)
    {
        double power;
        if(powerText==null || powerText.Length>64 ||
            !double.TryParse(powerText,NumberStyles.Float,CultureInfo.InvariantCulture,out power) ||
            double.IsNaN(power) || double.IsInfinity(power) || power<0 || power>100)
            throw new ArgumentOutOfRangeException("powerText","IR recipe power must be finite in 0..100.");
        Validate(root,edit,pid);
        if(Class(edit).IndexOf("EDIT",StringComparison.OrdinalIgnoreCase)<0 || (GetWindowLong(edit,-16)&0x0800)!=0)
            throw new InvalidOperationException("The IR recipe input is not a writable EDIT window.");
        IntPtr current=edit;
        for(int depth=0;;depth++)
        {
            if(current==IntPtr.Zero || depth>64 || !IsWindowEnabled(current))
                throw new InvalidOperationException("The recipe input or its parent is disabled/unverified.");
            if(current==root)break;
            current=GetParent(current);
        }
        UIntPtr result;
        // WM_SETTEXT is a standard marshalled system message. No registered
        // pointer message, remote allocation, attachment or process memory API.
        if(WriteMessage(edit,0x000C,UIntPtr.Zero,powerText,Flags,1000,out result)==IntPtr.Zero || result==UIntPtr.Zero)
            throw new InvalidOperationException("IR edit acknowledgement timed out or failed; the value may already be staged.");
        Validate(root,edit,pid);
    }
}
