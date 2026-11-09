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

    [DllImport("user32.dll")]
    private static extern int GetDlgCtrlID(IntPtr window);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassName(IntPtr window, StringBuilder value, int maximum);

    private static string NativeClass(IntPtr window)
    {
        var value = new StringBuilder(256);
        if (GetClassName(window, value, value.Capacity) == 0)
            throw new InvalidOperationException("A native identity class could not be verified.");
        return value.ToString();
    }

    private static string LeafId(IntPtr window)
    {
        BinderJetUiAutomationInitialization.Initialize();
        var cache = new CacheRequest {
            TreeScope = TreeScope.Element,
            AutomationElementMode = AutomationElementMode.None,
            TreeFilter = Condition.TrueCondition
        };
        cache.Add(AutomationElement.AutomationIdProperty);
        string id;
        using (cache.Activate()) {
            AutomationElement leaf = AutomationElement.FromHandle(window);
            if (leaf == null) throw new InvalidOperationException("A native leaf has no AutomationId provider.");
            id = Convert.ToString(leaf.GetCachedPropertyValue(AutomationElement.AutomationIdProperty), CultureInfo.InvariantCulture);
        }
        if (id == null || id.Length > 1024) throw new InvalidOperationException("A native leaf AutomationId exceeds the metadata bound.");
        return id;
    }

    public static Hashtable ScopeIdentity(IntPtr root, IntPtr scope, int expectedPid)
    {
        ValidateOwned(root, scope, expectedPid);
        if (scope == root) throw new InvalidOperationException("A non-root Sequence scope is required.");
        IntPtr parent = GetParent(scope);
        ValidateOwned(root, parent, expectedPid);
        string name = NativeClass(scope);
        uint owner; uint thread = GetWindowThreadProcessId(scope, out owner);
        int controlId = GetDlgCtrlID(scope);
        ValidateOwned(root, scope, expectedPid);
        uint afterOwner; uint afterThread = GetWindowThreadProcessId(scope, out afterOwner);
        if (GetParent(scope) != parent || NativeClass(scope) != name || thread == 0 || thread != afterThread || owner != afterOwner || GetDlgCtrlID(scope) != controlId)
            throw new InvalidOperationException("The Sequence scope changed during identity verification.");
        return new Hashtable { { "hwnd", scope.ToInt64() }, { "class_name", name }, { "parent", parent.ToInt64() }, { "thread", thread }, { "control_id", controlId } };
    }

    // Only structural HWND invalidation authorizes a separate visible re-audit.
    // No UIA/provider/field or global-label failure is converted to this status.
    public static bool ScopeStructureMatches(IntPtr root, IntPtr scope, IntPtr ir, IntPtr save,
        int expectedPid, string expectedClass, long expectedParent, long expectedThread,
        int expectedControlId, string expectedIrClass, string expectedSaveClass)
    {
        ValidateOwned(root, root, expectedPid);
        if (!HasProcess(scope, expectedPid) || scope == root || !IsChild(root, scope) ||
            !HasProcess(ir, expectedPid) || !HasProcess(save, expectedPid) ||
            !IsChild(scope, ir) || !IsChild(scope, save)) return false;
        // Unexpected provider or native read errors propagate as HOLD; only
        // positively observed structural differences permit a visible re-audit.
        Hashtable current = ScopeIdentity(root, scope, expectedPid);
        if ((string)current["class_name"] != expectedClass)
            throw new InvalidOperationException("The authenticated Sequence scope class changed.");
        if ((long)current["parent"] != expectedParent || Convert.ToInt64(current["thread"]) != expectedThread ||
            (int)current["control_id"] != expectedControlId) return false;
        if (NativeClass(ir) != expectedIrClass || NativeClass(save) != expectedSaveClass)
            throw new InvalidOperationException("An authenticated Sequence anchor class changed.");
        return CommonAncestor(root, ir, save, expectedPid) == scope;
    }

    public static Hashtable LeafIdentity(IntPtr root, IntPtr window, int expectedPid)
    {
        ValidateOwned(root, window, expectedPid);
        string name = NativeClass(window);
        IntPtr parent = GetParent(window);
        ValidateOwned(root, parent, expectedPid);
        uint owner; uint thread = GetWindowThreadProcessId(window, out owner);
        int controlId = GetDlgCtrlID(window);
        if (name.IndexOf("STATIC", StringComparison.OrdinalIgnoreCase) < 0 &&
            name.IndexOf("EDIT", StringComparison.OrdinalIgnoreCase) < 0 &&
            name.IndexOf("BUTTON", StringComparison.OrdinalIgnoreCase) < 0)
            throw new InvalidOperationException("An audited leaf class changed.");
        string id = LeafId(window);
        ValidateOwned(root, window, expectedPid);
        uint afterOwner; uint afterThread = GetWindowThreadProcessId(window, out afterOwner);
        if (NativeClass(window) != name || GetParent(window) != parent || thread == 0 || thread != afterThread ||
            owner != afterOwner || GetDlgCtrlID(window) != controlId)
            throw new InvalidOperationException("An audited leaf identity changed during identification.");
        return new Hashtable { { "hwnd", window.ToInt64() }, { "class_name", name }, { "id", id },
            { "parent", parent.ToInt64() }, { "thread", thread }, { "control_id", controlId } };
    }

    public static Hashtable[] CollectScoped(IntPtr root, IntPtr scope, int expectedPid)
    {
        ScopeIdentity(root, scope, expectedPid);
        IntPtr[] handles = BinderJetNativeProcess.RelativeLeafHandles(root, scope, expectedPid);
        var records = new List<Hashtable>();
        foreach (IntPtr window in handles) {
            string name = BinderJetNativeProcess.ValidateRelativeLeaf(root, scope, window, expectedPid);
            string id = LeafId(window);
            if (BinderJetNativeProcess.ValidateRelativeLeaf(root, scope, window, expectedPid) != name)
                throw new InvalidOperationException("A Sequence leaf changed class or inner visibility.");
            records.Add(new Hashtable { { "id", id }, { "hwnd", window.ToInt64() },
                { "class_name", name }, { "visible", IsWindowVisible(window) }, { "relative_visible", true },
                { "enabled", IsWindowEnabled(window) } });
        }
        var before = new HashSet<IntPtr>(handles);
        IntPtr[] after = BinderJetNativeProcess.RelativeLeafHandles(root, scope, expectedPid);
        if (before.Count != handles.Length || !before.SetEquals(after) || after.Length != handles.Length)
            throw new InvalidOperationException("The Sequence leaf inventory changed during collection.");
        ScopeIdentity(root, scope, expectedPid);
        return records.ToArray();
    }

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
