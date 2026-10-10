using Zagent.Shared;
using Zagent.Shared.Contracts;

namespace Zagent.Core;

public static class SampleManifests
{
    public static AgentManifest CreateDevelopmentEchoAgent(
        string agentId = "dev.echo")
    {
        return new AgentManifest(
            ProtocolVersion: ZagentProductInfo.AgentProtocolVersion,
            Agent: new AgentIdentity(
                Id: agentId,
                Name: "Development Echo Agent",
                Version: "0.1.0-dev"),
            Environment: AgentEnvironment.Development,
            Runtime: new AgentRuntimeDescriptor(
                Language: "dotnet",
                Version: "10.0",
                Entrypoint: "dev-echo"),
            Transport: new AgentTransportDescriptor(
                Kind: "internal",
                LocalOnly: true),
            Lifecycle: new AgentLifecycleDescriptor(),
            Health: new AgentHealthDescriptor(
                Probe: "custom"),
            Capabilities:
            [
                new CapabilityDescriptor(
                    Name: "dev.echo.ping",
                    Description: "Development capability used to verify registry contracts.",
                    Access: CapabilityAccess.Read,
                    Execution: CapabilityExecution.Fast,
                    Approval: CapabilityApproval.None,
                    SupportsCancel: false)
            ]);
    }
}
