// Built once for the external module. Never loaded into the BinderJet process.
using System.Runtime.CompilerServices;
using System.Windows.Automation;

public static class BinderJetUiAutomationInitialization
{
    [MethodImpl(MethodImplOptions.NoInlining)]
    public static void Initialize()
    {
        ClientSettings.RegisterClientSideProviders(new ClientSideProviderDescription[0]);
    }
}
