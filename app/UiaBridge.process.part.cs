using System;
using System.Collections;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using Accessibility;

/// <summary>
/// Target-scoped native bootstrap and cached text reads for Process mode.
/// No UI Automation types, initialization, provider cache or row Bounds access.
/// </summary>
public static class BinderJetNativeProcess
{
    private const int MaximumChildren = 900;
    private const int MaximumTextCharacters = 4096;
    private const uint TextMessage = 0x000D;
    private const uint TimeoutFlags = 0x0001 | 0x0002 | 0x0020;
    private delegate bool ChildCallback(IntPtr window, IntPtr parameter);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll")]
    private static extern bool IsChild(IntPtr parent, IntPtr child);
    [DllImport("user32.dll")]
    private static extern bool IsWindow(IntPtr window);
    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")]
    private static extern bool IsWindowEnabled(IntPtr window);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassName(IntPtr window, StringBuilder value, int maximum);
    [DllImport("user32.dll")]
    private static extern bool EnumChildWindows(IntPtr root, ChildCallback callback, IntPtr parameter);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr SendMessageTimeout(IntPtr window, uint message, UIntPtr wParam,
        StringBuilder lParam, uint flags, uint timeout, out UIntPtr result);
    [DllImport("oleacc.dll")]
    private static extern int AccessibleObjectFromWindow(IntPtr window, uint objectId,
        ref Guid interfaceId, [MarshalAs(UnmanagedType.Interface)] out object accessible);

    private static bool HasProcess(IntPtr window, int expectedPid)
    {
        if (window == IntPtr.Zero || expectedPid <= 0 || !IsWindow(window)) return false;
        uint process;
        return GetWindowThreadProcessId(window, out process) != 0 && process == (uint)expectedPid;
    }

    private static void ValidateRoot(IntPtr root, int expectedPid)
    {
        if (!HasProcess(root, expectedPid))
            throw new InvalidOperationException("The selected process/window identity changed.");
    }

    private static void ValidateChild(IntPtr root, IntPtr child, int expectedPid)
    {
        ValidateRoot(root, expectedPid);
        if (!HasProcess(child, expectedPid) || (child != root && !IsChild(root, child)))
            throw new InvalidOperationException("The cached native control no longer belongs to the selected window.");
    }

    private static string ClassName(IntPtr window)
    {
        var text = new StringBuilder(256);
        if (GetClassName(window, text, text.Capacity) == 0) return "";
        return text.ToString();
    }

    public static string ReadText(IntPtr root, IntPtr child, int expectedPid)
    {
        ValidateChild(root, child, expectedPid);
        var value = new StringBuilder(MaximumTextCharacters + 1);
        UIntPtr count;
        if (SendMessageTimeout(child, TextMessage, (UIntPtr)value.Capacity, value,
            TimeoutFlags, 250, out count) == IntPtr.Zero)
            throw new InvalidOperationException("The selected control did not answer WM_GETTEXT within 250 ms.");
        ValidateChild(root, child, expectedPid);
        if (count.ToUInt64() >= MaximumTextCharacters)
            throw new InvalidOperationException("Native control text exceeds the 4096-character read bound.");
        return value.ToString();
    }

    public static Hashtable[] Enumerate(IntPtr root, int expectedPid)
    {
        ValidateRoot(root, expectedPid);
        var handles = new List<IntPtr>();
        bool truncated = false;
        ChildCallback callback = delegate(IntPtr window, IntPtr unused)
        {
            if (handles.Count >= MaximumChildren) { truncated = true; return false; }
            if (HasProcess(window, expectedPid) && IsChild(root, window)) handles.Add(window);
            return true;
        };
        EnumChildWindows(root, callback, IntPtr.Zero);
        GC.KeepAlive(callback);
        if (truncated) throw new InvalidOperationException("Native control discovery exceeds the 900-child bound.");
        var result = new List<Hashtable>();
        foreach (IntPtr window in handles)
        {
            ValidateChild(root, window, expectedPid);
            string className = ClassName(window);
            bool visible = IsWindowVisible(window);
            // Inventory does not collect unrelated edit values. Explicit cached
            // ReadText calls remain available for an audited status/progress HWND.
            bool readable = visible && (className.IndexOf("STATIC", StringComparison.OrdinalIgnoreCase) >= 0 ||
                className.IndexOf("BUTTON", StringComparison.OrdinalIgnoreCase) >= 0);
            result.Add(new Hashtable {
                { "hwnd", window.ToInt64() }, { "class_name", className },
                { "text", readable ? ReadText(root, window, expectedPid) : "" },
                { "visible", visible }, { "enabled", IsWindowEnabled(window) }
            });
        }
        ValidateRoot(root, expectedPid);
        return result.ToArray();
    }

    private static string AccessibleName(IntPtr window)
    {
        object value = null;
        try
        {
            Guid iid = new Guid("618736e0-3c3d-11cf-810c-00aa00389b71");
            if (AccessibleObjectFromWindow(window, unchecked((uint)-4), ref iid, out value) != 0) return "";
            var accessible = value as IAccessible;
            return accessible == null ? "" : accessible.get_accName(0) ?? "";
        }
        catch (COMException) { return ""; }
        finally { if (value != null && Marshal.IsComObject(value)) Marshal.ReleaseComObject(value); }
    }

    public static IntPtr FindNavigation(IntPtr root, int expectedPid, string id)
    {
        string expected = id == "btnProcess" ? "Process" : id == "btnSequences" ? "Sequences" : null;
        if (expected == null) throw new ArgumentException("Only the audited Process/Sequences navigation buttons are supported.", "id");
        IntPtr found = IntPtr.Zero;
        foreach (Hashtable control in Enumerate(root, expectedPid))
        {
            if (!(bool)control["visible"] || !(bool)control["enabled"] ||
                ((string)control["class_name"]).IndexOf("BUTTON", StringComparison.OrdinalIgnoreCase) < 0) continue;
            IntPtr window = new IntPtr((long)control["hwnd"]);
            string text = ((string)control["text"]).Trim();
            bool matches = string.Equals(text, expected, StringComparison.OrdinalIgnoreCase);
            if (!matches) matches = string.Equals(AccessibleName(window).Trim(), expected, StringComparison.OrdinalIgnoreCase);
            if (!matches) continue;
            if (found != IntPtr.Zero) throw new InvalidOperationException("The audited navigation caption is ambiguous.");
            found = window;
        }
        if (found == IntPtr.Zero) throw new InvalidOperationException("The audited visible navigation button is unavailable.");
        ValidateChild(root, found, expectedPid);
        return found;
    }

    public static string IdentifyMachine(IntPtr root, int expectedPid)
    {
        string found = null;
        foreach (Hashtable control in Enumerate(root, expectedPid))
        {
            if (!(bool)control["visible"] ||
                ((string)control["class_name"]).IndexOf("STATIC", StringComparison.OrdinalIgnoreCase) < 0) continue;
            string text = ((string)control["text"]).Trim().ToUpperInvariant();
            if (text != "M1" && text != "S3") continue;
            if (found != null && found != text) throw new InvalidOperationException("The selected window exposes conflicting machine labels.");
            found = text;
        }
        return found ?? "unknown";
    }

    public static IntPtr FindGrid(IntPtr root, int expectedPid, int currentRow)
    {
        return FindGrid(root,expectedPid,currentRow,false);
    }

    public static IntPtr FindGrid(IntPtr root, int expectedPid, int currentRow, bool includeHidden)
    {
        IntPtr found = IntPtr.Zero;
        foreach (Hashtable control in Enumerate(root, expectedPid))
        {
            string className = (string)control["class_name"];
            if ((!includeHidden && !(bool)control["visible"]) ||
                (className.IndexOf("WindowsForms", StringComparison.OrdinalIgnoreCase) < 0 &&
                 className.IndexOf("DataGrid", StringComparison.OrdinalIgnoreCase) < 0) ||
                className.IndexOf("BUTTON", StringComparison.OrdinalIgnoreCase) >= 0 ||
                className.IndexOf("STATIC", StringComparison.OrdinalIgnoreCase) >= 0 ||
                className.IndexOf("EDIT", StringComparison.OrdinalIgnoreCase) >= 0) continue;
            IntPtr window = new IntPtr((long)control["hwnd"]);
            bool matches = false;
            try { matches = BinderJetAccessibleGrid.HasRecoatingHeaders(window); }
            catch (COMException) { }
            catch (InvalidOperationException) { }
            if (!matches) continue;
            if (found != IntPtr.Zero) throw new InvalidOperationException("Multiple native T_recoating tables are visible.");
            found = window;
        }
        if (found != IntPtr.Zero) ValidateChild(root, found, expectedPid);
        return found;
    }
}
