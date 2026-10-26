using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

/// <summary>
/// Queues only an audited child BUTTON click. Does not focus a window, inject
/// code, send keyboard input, or synchronously re-enter the target UI thread.
/// The caller must verify the resulting page before continuing.
/// </summary>
public static class BinderJetQueuedNavigation
{
    [StructLayout(LayoutKind.Sequential)]
    private struct NativeRect { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    private struct GuiThreadInfo
    {
        public uint Size, Flags;
        public IntPtr Active, Focus, Capture, MenuOwner, MoveSize, Caret;
        public NativeRect CaretRect;
    }

    private const uint ButtonClick = 0x00F5;
    private const uint NullMessage = 0;
    private const uint AbortIfHung = 0x0002;
    private const uint BlockReentrancy = 0x0001;
    private const uint ErrorOnExit = 0x0020;

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern bool GetGUIThreadInfo(uint thread, ref GuiThreadInfo info);

    [DllImport("user32.dll")]
    private static extern IntPtr GetAncestor(IntPtr window, uint flags);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsChild(IntPtr parent, IntPtr child);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsWindowVisible(IntPtr window);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr window);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsWindowEnabled(IntPtr window);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassName(IntPtr window, StringBuilder className, int maximum);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SendMessageTimeout(IntPtr window, uint message,
        IntPtr wParam, IntPtr lParam, uint flags, uint timeout, out UIntPtr result);

    private static bool HasIdentity(IntPtr window, int expectedProcess)
    {
        if (window == IntPtr.Zero || expectedProcess <= 0) return false;
        uint process;
        uint thread = GetWindowThreadProcessId(window, out process);
        return thread != 0 && process == (uint)expectedProcess;
    }

    public static void Click(IntPtr root, IntPtr button, int expectedPid)
    {
        if (!HasIdentity(root, expectedPid) || !HasIdentity(button, expectedPid) || !IsChild(root, button))
            throw new InvalidOperationException("The audited navigation button/window identity changed.");
        if (!IsWindowVisible(root) || !IsWindowVisible(button) || !IsWindowEnabled(root) || !IsWindowEnabled(button))
            throw new InvalidOperationException("The audited navigation button is not visible and enabled.");
        var className = new StringBuilder(256);
        if (GetClassName(button, className, className.Capacity) == 0 ||
            className.ToString().IndexOf("BUTTON", StringComparison.OrdinalIgnoreCase) < 0)
            throw new InvalidOperationException("The navigation handle is not a verified native BUTTON.");
        if (!PostMessage(button, ButtonClick, IntPtr.Zero, IntPtr.Zero))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "The navigation click could not be queued.");
    }

    public static bool Responsive(IntPtr root, int expectedPid, int timeoutMs)
    {
        if (timeoutMs < 1 || timeoutMs > 1000)
            throw new ArgumentOutOfRangeException("timeoutMs", "Use a bounded timeout of 1..1000 ms.");
        if (!HasIdentity(root, expectedPid)) return false;
        UIntPtr result;
        return SendMessageTimeout(root, NullMessage, IntPtr.Zero, IntPtr.Zero,
            AbortIfHung | BlockReentrancy | ErrorOnExit, (uint)timeoutMs, out result) != IntPtr.Zero;
    }

    /// <summary>Inspect only this target's native editing focus; never query a global UIA provider.</summary>
    public static bool IsUserEditing(IntPtr root, int expectedPid)
    {
        if (!HasIdentity(root, expectedPid))
            throw new InvalidOperationException("The editing-focus target window identity changed.");
        IntPtr foreground = GetForegroundWindow();
        uint process;
        uint thread = GetWindowThreadProcessId(foreground, out process);
        // This fast path deliberately does not inspect another application's
        // GUI thread, child window, accessibility object, or control class.
        if (foreground == IntPtr.Zero || thread == 0 || process != (uint)expectedPid) return false;

        var info = new GuiThreadInfo { Size = (uint)Marshal.SizeOf(typeof(GuiThreadInfo)) };
        if (!GetGUIThreadInfo(thread, ref info))
            throw new InvalidOperationException("The target's native editing focus is unavailable.");
        if (info.Focus == IntPtr.Zero) return false;
        if (!HasIdentity(info.Focus, expectedPid))
            throw new InvalidOperationException("The target's editing focus belongs to an unverified window.");
        if (info.Focus != root && !IsChild(root, info.Focus) && GetAncestor(info.Focus, 3) != root) return false;
        var className = new StringBuilder(256);
        if (GetClassName(info.Focus, className, className.Capacity) == 0)
            throw new InvalidOperationException("The target's focused control class is unavailable.");
        return className.ToString().IndexOf("EDIT", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    /// <summary>Use native ownership/visibility only; UIA IsOffscreen can enter legacy grid Bounds recursion.</summary>
    public static bool IsVisible(IntPtr root, IntPtr child, int expectedPid)
    {
        return HasIdentity(root, expectedPid) && HasIdentity(child, expectedPid) &&
            (root == child || IsChild(root, child)) && !IsIconic(root) &&
            IsWindowVisible(root) && IsWindowVisible(child);
    }

    public static bool IsOwned(IntPtr root, IntPtr child, int expectedPid)
    {
        return HasIdentity(root, expectedPid) && HasIdentity(child, expectedPid) &&
            (root == child || IsChild(root, child));
    }

    public static bool IsShown(IntPtr root, IntPtr child, int expectedPid)
    {
        return IsOwned(root,child,expectedPid) && IsWindowVisible(root) && IsWindowVisible(child);
    }
}
