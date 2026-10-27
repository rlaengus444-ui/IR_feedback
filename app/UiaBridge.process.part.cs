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
    private const int MaximumWindowVisits = 16384;
    private const int MaximumTextCharacters = 4096;
    private const uint TextMessage = 0x000D;
    private const uint TimeoutFlags = 0x0001 | 0x0002 | 0x0020;
    private delegate bool ChildCallback(IntPtr window, IntPtr parameter);
    [StructLayout(LayoutKind.Sequential)] private struct Rect { public int Left,Top,Right,Bottom; }
    [DllImport("user32.dll")] private static extern bool EnumWindows(ChildCallback callback,IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr window,out Rect rect);
    [DllImport("user32.dll")] private static extern IntPtr GetWindow(IntPtr window,uint command);

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

    private static bool IsLeafClass(string className, bool includeEdits)
    {
        return className.IndexOf("STATIC", StringComparison.OrdinalIgnoreCase) >= 0 ||
            className.IndexOf("BUTTON", StringComparison.OrdinalIgnoreCase) >= 0 ||
            (includeEdits && className.IndexOf("EDIT", StringComparison.OrdinalIgnoreCase) >= 0);
    }

    // Native enumeration is bounded separately from the eligible leaf inventory.
    // Hidden views and non-leaf containers must not exhaust the 900-control
    // budget or receive WM_GETTEXT/UIA requests. Always finish the bounded scan
    // before a caller selects a match, so late duplicates cannot be hidden.
    internal static IntPtr[] VisibleLeafHandles(IntPtr root, int expectedPid, bool includeEdits)
    {
        ValidateRoot(root, expectedPid);
        var handles = new List<IntPtr>();
        int visits = 0;
        bool visitLimit = false, candidateLimit = false, unreadableClass = false;
        ChildCallback callback = delegate(IntPtr window, IntPtr unused)
        {
            if (++visits > MaximumWindowVisits) { visitLimit = true; return false; }
            if (!HasProcess(window, expectedPid) || !IsChild(root, window) || !IsWindowVisible(window)) return true;
            string className = ClassName(window);
            if (className.Length == 0) { unreadableClass = true; return false; }
            if (!IsLeafClass(className, includeEdits)) return true;
            if (handles.Count >= MaximumChildren) { candidateLimit = true; return false; }
            handles.Add(window);
            return true;
        };
        EnumChildWindows(root, callback, IntPtr.Zero);
        GC.KeepAlive(callback);
        if (visitLimit) throw new InvalidOperationException("Native window discovery exceeds the 16384-visit bound; no partial inventory was used.");
        if (candidateLimit) throw new InvalidOperationException("Visible native leaf discovery exceeds the 900-control bound; no partial inventory was used.");
        if (unreadableClass) throw new InvalidOperationException("A visible native control class could not be verified.");
        ValidateRoot(root, expectedPid);
        return handles.ToArray();
    }

    internal static string ValidateVisibleLeaf(IntPtr root, IntPtr window, int expectedPid, bool includeEdits)
    {
        ValidateChild(root, window, expectedPid);
        string className = ClassName(window);
        if (!IsWindowVisible(window) || !IsLeafClass(className, includeEdits))
            throw new InvalidOperationException("A native discovery candidate is no longer a visible supported leaf.");
        return className;
    }

    public static Hashtable[] Enumerate(IntPtr root, int expectedPid)
    {
        IntPtr[] handles = VisibleLeafHandles(root, expectedPid, false);
        var result = new List<Hashtable>();
        foreach (IntPtr window in handles)
        {
            string className = ValidateVisibleLeaf(root, window, expectedPid, false);
            // Inventory does not collect unrelated edit values. Explicit cached
            // ReadText calls remain available for an audited status/progress HWND.
            string text = ReadText(root, window, expectedPid);
            if (ValidateVisibleLeaf(root, window, expectedPid, false) != className)
                throw new InvalidOperationException("A native discovery candidate class changed during its text read.");
            result.Add(new Hashtable {
                { "hwnd", window.ToInt64() }, { "class_name", className },
                { "text", text },
                { "visible", true }, { "enabled", IsWindowEnabled(window) }
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
        string expected = id == "btnSequences" ? "Sequences" : null;
        if (expected == null) throw new ArgumentException("Only the audited Sequences navigation button is supported.", "id");
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

    public static void ValidateWindow(IntPtr root, int expectedPid) { ValidateRoot(root,expectedPid); }

    public static IntPtr FindRoot(int expectedPid)
    {
        IntPtr selected=IntPtr.Zero;long largest=-1;
        ChildCallback callback=delegate(IntPtr window,IntPtr unused) {
            if(!HasProcess(window,expectedPid)||GetWindow(window,4)!=IntPtr.Zero)return true;
            if(ClassName(window).IndexOf("WindowsForms",StringComparison.OrdinalIgnoreCase)<0)return true;
            Rect rect;if(!GetWindowRect(window,out rect))return true;
            long area=(long)Math.Max(0,rect.Right-rect.Left)*Math.Max(0,rect.Bottom-rect.Top);
            if(IsWindowVisible(window))area+=1L<<40;
            if(area>largest){largest=area;selected=window;}
            return true;
        };
        EnumWindows(callback,IntPtr.Zero);GC.KeepAlive(callback);return selected;
    }
}
