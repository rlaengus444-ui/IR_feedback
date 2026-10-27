using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Windows.Automation;

/// <summary>
/// AutomationId metadata for owned, native leaf controls only. The root, forms,
/// grids and their UIA trees are never materialized. All later field operations
/// use the returned HWNDs and native messages instead of UIA patterns.
/// </summary>
public static class BinderJetLeafMetadata
{
    private const int MaximumParentDepth = 64;

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

    public static Hashtable[] Collect(IntPtr root, int expectedPid)
    {
        ValidateOwned(root, root, expectedPid);
        IntPtr[] handles = BinderJetNativeProcess.VisibleLeafHandles(root, expectedPid, true);

        BinderJetUiAutomationInitialization.Initialize();
        var records = new List<Hashtable>();
        foreach (IntPtr window in handles)
        {
            string className = BinderJetNativeProcess.ValidateVisibleLeaf(root, window, expectedPid, true);

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
            if (BinderJetNativeProcess.ValidateVisibleLeaf(root, window, expectedPid, true) != className)
                throw new InvalidOperationException("A native leaf changed visibility/class during metadata collection.");
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
