using System;
using System.Runtime.InteropServices;
using System.Text;

/// <summary>Queue bounded standard view scrolling without entering a UIA provider callback.</summary>
public static class BinderJetQueuedGridScroll
{
    [StructLayout(LayoutKind.Sequential)]
    private struct ScrollInfo
    {
        public uint Size, Mask;
        public int Minimum, Maximum;
        public uint Page;
        public int Position, TrackPosition;
    }

    [DllImport("user32.dll")] private static extern IntPtr GetWindow(IntPtr window, uint command);
    [DllImport("user32.dll")] private static extern IntPtr GetParent(IntPtr window);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(IntPtr window, StringBuilder text, int count);
    [DllImport("user32.dll")] private static extern int GetWindowLong(IntPtr window, int index);
    [DllImport("user32.dll")] private static extern bool GetScrollInfo(IntPtr window, int bar, ref ScrollInfo info);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll")] private static extern bool IsChild(IntPtr parent, IntPtr child);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);

    private static bool Owned(IntPtr window, int expectedPid)
    {
        uint process;
        return window != IntPtr.Zero && GetWindowThreadProcessId(window, out process) != 0 && process == (uint)expectedPid;
    }

    private static void Queue(IntPtr grid, IntPtr scrollbar, int code)
    {
        if (!PostMessage(grid, 0x0115, new IntPtr(code), scrollbar))
            throw new InvalidOperationException("The verified grid did not accept a queued view-scroll message.");
    }

    public static int Follow(IntPtr root, IntPtr grid, int expectedPid, int currentRow, int rowCount)
    {
        if (expectedPid <= 0 || rowCount <= 0 || currentRow < 0 || currentRow >= rowCount)
            throw new ArgumentOutOfRangeException("currentRow", "The requested row must belong to the verified layer grid.");
        if (!Owned(root, expectedPid) || !Owned(grid, expectedPid) || !IsChild(root, grid) || !IsWindowVisible(grid))
            throw new InvalidOperationException("The visible grid no longer belongs to the selected process/window.");

        IntPtr child = GetWindow(grid, 5); // GW_CHILD, then direct siblings only.
        for (int index = 0; child != IntPtr.Zero && index < 32; index++, child = GetWindow(child, 2))
        {
            var className = new StringBuilder(256);
            GetClassName(child, className, className.Capacity);
            if (className.ToString().IndexOf("SCROLLBAR", StringComparison.OrdinalIgnoreCase) < 0 ||
                (GetWindowLong(child, -16) & 1) == 0) // GWL_STYLE / SBS_VERT.
                continue;
            if (!Owned(child, expectedPid) || GetParent(child) != grid || !IsWindowVisible(child))
                continue;

            var info = new ScrollInfo { Size = (uint)Marshal.SizeOf(typeof(ScrollInfo)), Mask = 7 };
            if (!GetScrollInfo(child, 2, ref info)) // SB_CTL; range/page/current position are public 32-bit values.
                throw new InvalidOperationException("The verified scrollbar's public range is unavailable.");
            double range = (double)info.Maximum - info.Minimum + 1;
            if (info.Minimum < 0 || range <= 0 || info.Page == 0 || info.Page > range)
                throw new InvalidOperationException("The verified scrollbar's public range is inconsistent.");
            int maximum = (int)Math.Max(info.Minimum, (long)info.Maximum - info.Page + 1);
            if (info.Position < info.Minimum || info.Position > maximum)
                throw new InvalidOperationException("The verified scrollbar's current position is inconsistent.");
            double visibleRows = Math.Max(1, Math.Floor(rowCount * (double)info.Page / range));
            if (rowCount <= visibleRows) return 0;
            int target = info.Minimum + (int)Math.Round(((double)maximum - info.Minimum) * currentRow /
                                                        Math.Max(1, rowCount - visibleRows));
            target = Math.Max(info.Minimum, Math.Min(maximum, target));

            // Keep progress across polls. Use an end anchor only when it is
            // closer than the current position; never reset an in-progress seek.
            int start = info.Position, anchor = -1;
            if (Math.Abs((double)target - info.Minimum) < Math.Abs((double)target - start))
            { start = info.Minimum; anchor = 6; } // SB_TOP.
            if (Math.Abs((double)maximum - target) < Math.Abs((double)target - start))
            { start = maximum; anchor = 7; } // SB_BOTTOM.
            int messages = 0;
            if (anchor >= 0) { Queue(grid, child, anchor); messages++; }
            int pages = (int)Math.Min(64 - messages, Math.Round(Math.Abs((double)target - start) / info.Page));
            int direction = target >= start ? 3 : 2; // SB_PAGEDOWN / SB_PAGEUP.
            for (int page = 0; page < pages; page++) { Queue(grid, child, direction); messages++; }
            return messages;
        }
        throw new InvalidOperationException("The selected grid has no verified visible vertical scrollbar.");
    }
}
