using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Automation;

/// <summary>
/// AutomationId metadata for owned, native leaf controls only. The root, forms,
/// grids and their UIA trees are never materialized. All later field operations
/// use the returned HWNDs and native messages instead of UIA patterns.
/// </summary>
public static class BinderJetLeafMetadata
{
    private const int MaximumChildren = 900;
    private const int MaximumParentDepth = 64;
    private delegate bool ChildCallback(IntPtr window, IntPtr parameter);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll")]
    private static extern bool IsWindow(IntPtr window);
    [DllImport("user32.dll")]
    private static extern bool IsChild(IntPtr parent, IntPtr child);
    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")]
    private static extern bool IsWindowEnabled(IntPtr window);
    [DllImport("user32.dll")]
    private static extern IntPtr GetParent(IntPtr window);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassName(IntPtr window, StringBuilder value, int maximum);
    [DllImport("user32.dll")]
    private static extern bool EnumChildWindows(IntPtr root, ChildCallback callback, IntPtr parameter);

    private static bool HasProcess(IntPtr window, int expectedPid)
    {
        if (window == IntPtr.Zero || expectedPid <= 0 || !IsWindow(window)) return false;
        uint process;
        return GetWindowThreadProcessId(window, out process) != 0 && process == (uint)expectedPid;
    }

    private static void ValidateOwned(IntPtr root, IntPtr window, int expectedPid)
    {
        if (!HasProcess(root, expectedPid) || !HasProcess(window, expectedPid) ||
            (window != root && !IsChild(root, window)))
            throw new InvalidOperationException("The selected native control/window identity changed.");
    }

    private static string ClassName(IntPtr window)
    {
        var value = new StringBuilder(256);
        if (GetClassName(window, value, value.Capacity) == 0)
            throw new InvalidOperationException("A native leaf control class could not be read.");
        return value.ToString();
    }

    private static bool IsSupportedLeafClass(string className)
    {
        return className.IndexOf("EDIT", StringComparison.OrdinalIgnoreCase) >= 0 ||
            className.IndexOf("BUTTON", StringComparison.OrdinalIgnoreCase) >= 0 ||
            className.IndexOf("STATIC", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    public static Hashtable[] Collect(IntPtr root, int expectedPid)
    {
        ValidateOwned(root, root, expectedPid);
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
        if (truncated)
            throw new InvalidOperationException("Native leaf discovery exceeds the 900-child bound.");

        BinderJetUiAutomationInitialization.Initialize();
        var records = new List<Hashtable>();
        foreach (IntPtr window in handles)
        {
            ValidateOwned(root, window, expectedPid);
            string className = ClassName(window);
            if (!IsSupportedLeafClass(className)) continue;

            // Element-only cached ID access is intentional. Do not replace with
            // Current, FindAll, tree walkers, root FromHandle, bounds or patterns.
            var cache = new CacheRequest {
                TreeScope = TreeScope.Element,
                AutomationElementMode = AutomationElementMode.None,
                TreeFilter = Condition.TrueCondition
            };
            cache.Add(AutomationElement.AutomationIdProperty);
            string id;
            using (cache.Activate())
            {
                AutomationElement leaf = AutomationElement.FromHandle(window);
                if (leaf == null)
                    throw new InvalidOperationException("A native leaf has no AutomationId provider.");
                id = Convert.ToString(leaf.GetCachedPropertyValue(AutomationElement.AutomationIdProperty),
                    CultureInfo.InvariantCulture);
            }
            ValidateOwned(root, window, expectedPid);
            if (id.Length > 1024)
                throw new InvalidOperationException("A native leaf AutomationId exceeds the metadata bound.");
            records.Add(new Hashtable {
                { "id", id }, { "hwnd", window.ToInt64() }, { "class_name", className },
                { "visible", IsWindowVisible(window) }, { "enabled", IsWindowEnabled(window) }
            });
        }
        ValidateOwned(root, root, expectedPid);
        return records.ToArray();
    }

    public static IntPtr CommonAncestor(IntPtr root, IntPtr first, IntPtr second, int expectedPid)
    {
        ValidateOwned(root, first, expectedPid);
        ValidateOwned(root, second, expectedPid);
        var firstAncestors = new HashSet<IntPtr>();
        IntPtr current = first;
        bool reachedRoot = false;
        for (int depth = 0; depth < MaximumParentDepth; depth++)
        {
            ValidateOwned(root, current, expectedPid);
            firstAncestors.Add(current);
            if (current == root) { reachedRoot = true; break; }
            current = GetParent(current);
        }
        if (!reachedRoot)
            throw new InvalidOperationException("The first native control parent chain exceeds 64 levels.");

        current = second;
        for (int depth = 0; depth < MaximumParentDepth; depth++)
        {
            ValidateOwned(root, current, expectedPid);
            if (firstAncestors.Contains(current)) return current;
            if (current == root) break;
            current = GetParent(current);
        }
        throw new InvalidOperationException("No bounded, owned common native ancestor was found.");
    }

    public static bool IsDescendant(IntPtr root, IntPtr scope, IntPtr child, int expectedPid)
    {
        if (!HasProcess(root, expectedPid) || !HasProcess(scope, expectedPid) ||
            !HasProcess(child, expectedPid)) return false;
        if ((scope != root && !IsChild(root, scope)) ||
            (child != root && !IsChild(root, child))) return false;
        return child == scope || IsChild(scope, child);
    }
}
