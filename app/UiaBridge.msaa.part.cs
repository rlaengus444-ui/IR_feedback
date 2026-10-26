using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using Accessibility;

/// <summary>Read-only indexed MSAA fallback for legacy WinForms DataGridView providers.</summary>
public static class BinderJetAccessibleGrid
{
    [DllImport("oleacc.dll")]
    private static extern int AccessibleObjectFromWindow(IntPtr window, uint objectId, ref Guid interfaceId,
        [MarshalAs(UnmanagedType.Interface)] out object accessible);

    private static IAccessible Client(IntPtr window)
    {
        Guid iid = new Guid("618736e0-3c3d-11cf-810c-00aa00389b71");
        object value;
        int result = AccessibleObjectFromWindow(window, unchecked((uint)-4), ref iid, out value);
        if (result != 0 || !(value is IAccessible)) throw new InvalidOperationException("The grid MSAA client is unavailable.");
        return (IAccessible)value;
    }

    private static IAccessible Child(IAccessible parent, int index)
    {
        try { return parent.get_accChild(index) as IAccessible; }
        catch (COMException) { return null; }
    }

    private static string Name(IAccessible value)
    {
        try { return value == null ? "" : value.get_accName(0) ?? ""; }
        catch (COMException) { return ""; }
    }

    private static string Value(IAccessible parent, int index)
    {
        IAccessible child = Child(parent, index);
        try { return child == null ? parent.get_accValue(index) ?? "" : child.get_accValue(0) ?? ""; }
        catch (COMException) { return ""; }
    }

    private static int TrailingIndex(string name)
    {
        Match match = Regex.Match(name ?? "", @"(?:^|\D)(\d+)\s*$");
        int value;
        return match.Success && int.TryParse(match.Groups[1].Value, NumberStyles.None, CultureInfo.InvariantCulture, out value) ? value : -1;
    }

    public static bool HasRecoatingHeaders(IntPtr window)
    {
        IAccessible grid = Client(window);
        if (Convert.ToInt32(grid.get_accRole(0), CultureInfo.InvariantCulture) != 0x18) return false;
        for (int index = 1; index <= Math.Min(grid.accChildCount, 8); index++)
        {
            IAccessible header = Child(grid, index);
            if (header == null) continue;
            bool temperature = false, start = false, id = false;
            for (int cell = 1; cell <= Math.Min(header.accChildCount, 32); cell++)
            {
                string name = Name(Child(header, cell)).Replace("_", "").Replace(" ", "").ToLowerInvariant();
                temperature |= name == "trecoating";
                start |= name == "start";
                id |= name == "id" || name == "#" || name == "layer";
            }
            if (temperature && start && id) return true;
        }
        return false;
    }

    public static Hashtable[] ReadRows(IntPtr window, int currentRow)
    {
        var rows = new List<Hashtable>();
        IAccessible grid = Client(window);
        int children = grid.accChildCount;
        int firstDataChild = -1, firstRowName = -1, temperatureCell = -1, startCell = -1, idCell = -1;
        // Discover the actual public header hierarchy, rather than assuming a
        // private control layout or an unverified child-index offset.
        for (int index = 1; index <= Math.Min(children, 8); index++)
        {
            IAccessible header = Child(grid, index);
            if (header == null) continue;
            for (int cell = 1; cell <= Math.Min(header.accChildCount, 32); cell++)
            {
                string name = Name(Child(header, cell)).Replace("_", "").Replace(" ", "").ToLowerInvariant();
                if (name == "trecoating") temperatureCell = cell;
                if (name == "start") startCell = cell;
                if (name == "id" || name == "#" || name == "layer") idCell = cell;
            }
            if (temperatureCell < 0 || startCell < 0 || idCell < 0) continue;
            firstDataChild = index + 1;
            firstRowName = TrailingIndex(Name(Child(grid, firstDataChild)));
            if (firstRowName < 0) return rows.ToArray();
            break;
        }
        if (firstDataChild < 0) return rows.ToArray();
        int rowCount = 0;
        // A legacy grid can expose scrollbars after its rows. Verify a final
        // row's public ordinal instead of asking UIA GridPattern for a count.
        for (int index = children; index >= Math.Max(firstDataChild, children - 8); index--)
        {
            if (TrailingIndex(Name(Child(grid, index))) == firstRowName + index - firstDataChild)
            {
                rowCount = index - firstDataChild + 1;
                break;
            }
        }
        if (rowCount <= 0) return rows.ToArray();
        int[] requested = currentRow > 0 ? new[] { currentRow - 1, currentRow } : new[] { 0 };
        foreach (int rowIndex in requested)
        {
            int childIndex = checked(firstDataChild + rowIndex);
            if (childIndex > children) continue;
            IAccessible row = Child(grid, childIndex);
            if (row == null || TrailingIndex(Name(row)) != firstRowName + rowIndex) continue;
            if (Math.Max(temperatureCell, Math.Max(startCell, idCell)) > row.accChildCount) continue;
            rows.Add(new Hashtable {
                { "row_index", rowIndex }, { "temperature", Value(row, temperatureCell) },
                { "start", Value(row, startCell) }, { "layer_id", Value(row, idCell) },
                { "row_count", rowCount }
            });
        }
        return rows.ToArray();
    }

    public static string Describe(IntPtr window)
    {
        var text = new StringBuilder();
        IAccessible grid = Client(window);
        text.Append("children=").Append(grid.accChildCount);
        for (int index = 1; index <= Math.Min(grid.accChildCount, 5); index++)
        {
            IAccessible row = Child(grid, index);
            text.Append("; child").Append(index).Append('=').Append(Name(row));
            if (row == null) continue;
            text.Append(" count=").Append(row.accChildCount);
            for (int cell = 1; cell <= Math.Min(row.accChildCount, 6); cell++)
                text.Append(" [").Append(cell).Append(':').Append(Name(Child(row, cell))).Append('=').Append(Value(row, cell)).Append(']');
        }
        return text.ToString();
    }
}
