using System.Net.Http.Json;
using System.Net.Sockets;
using System.Text.Json;
using System.Text.Json.Nodes;
using Zagent.Shared.Manager;

namespace Zagent.Core.Manager;

public sealed class RouterManagerService
{
    private readonly HttpClient _httpClient;
    private readonly Uri _baseUri;
    private readonly string _configRoot;
    private readonly SemaphoreSlim _configGate = new(1, 1);

    public RouterManagerService(
        Uri baseUri,
        string configRoot,
        HttpClient? httpClient = null)
    {
        ArgumentNullException.ThrowIfNull(baseUri);
        ArgumentException.ThrowIfNullOrWhiteSpace(configRoot);

        if (!baseUri.IsLoopback)
        {
            throw new ArgumentException(
                "Router Manager endpoint must be loopback.",
                nameof(baseUri));
        }

        _baseUri = new Uri(
            baseUri.GetLeftPart(UriPartial.Authority) + "/");
        _configRoot = Path.GetFullPath(configRoot);
        _httpClient = httpClient ?? new HttpClient(
            new SocketsHttpHandler
            {
                UseProxy = false
            })
        {
            Timeout = TimeSpan.FromSeconds(5)
        };
    }

    public async Task<(string TrustState, string TargetValidationMode, bool NoSilentFallback, IReadOnlyList<ManagerIdentitySessionBindingRow> Bindings, IReadOnlyList<ManagerIdentityDeviceRow> Devices, string RegistryScope, bool SharedAuthorityConfigured)> IdentitySnapshotAsync(
        CancellationToken cancellationToken = default)
    {
        using var identityDocument = await GetJsonAsync(
            "router/identity",
            cancellationToken);
        using var bindingsDocument = await GetJsonAsync(
            "router/identity/bindings",
            cancellationToken);
        using var devicesDocument = await GetJsonAsync(
            "router/identity/devices",
            cancellationToken);

        var identityRoot = identityDocument.RootElement;
        var trustState =
            GetString(
                identityRoot.GetProperty("device"),
                "trust_state") ??
            "unverified";
        var targetValidation =
            GetString(
                identityRoot,
                "enforcement_mode") ??
            "compatibility";
        var noSilentFallback =
            GetBool(
                identityRoot,
                "no_silent_fallback",
                true);

        var registryScope =
            GetString(identityRoot, "registry_scope") ?? "";
        var sharedAuthorityConfigured =
            GetBool(identityRoot, "shared_authority_configured", false);

        var bindings = new List<ManagerIdentitySessionBindingRow>();
        if (bindingsDocument.RootElement.TryGetProperty(
                "bindings",
                out var rows) &&
            rows.ValueKind == JsonValueKind.Array)
        {
            foreach (var row in rows.EnumerateArray())
            {
                bindings.Add(
                    new ManagerIdentitySessionBindingRow(
                        GetString(row, "session_id") ?? "",
                        GetString(row, "operator_alias") ?? "",
                        GetString(row, "target_device_id") ?? "",
                        GetString(row, "device_alias") ?? "",
                        GetInt(row, "revision") ?? 0,
                        FromUnixSeconds(
                            row,
                            "bound_at"),
                        FromUnixSeconds(
                            row,
                            "updated_at"),
                        GetString(row, "binding_state") ??
                        "bound"));
            }
        }

        var devices = new List<ManagerIdentityDeviceRow>();
        if (devicesDocument.RootElement.TryGetProperty(
                "devices",
                out var deviceRows) &&
            deviceRows.ValueKind == JsonValueKind.Array)
        {
            foreach (var row in deviceRows.EnumerateArray())
            {
                devices.Add(
                    new ManagerIdentityDeviceRow(
                        GetString(row, "device_id") ?? "",
                        GetString(row, "device_name") ?? "",
                        GetString(row, "device_alias") ?? "",
                        GetString(row, "short_fingerprint") ?? "",
                        GetString(row, "trust_state") ?? "unverified",
                        GetBool(row, "trusted", false),
                        GetBool(row, "online", false),
                        FromUnixSeconds(row, "last_seen"),
                        GetString(row, "router_id") ?? "",
                        GetBool(row, "route_available", false)));
            }
        }

        return (
            trustState,
            targetValidation,
            noSilentFallback,
            bindings,
            devices,
            registryScope,
            sharedAuthorityConfigured);
    }

    public async Task RenameIdentityAliasAsync(
        string deviceAlias,
        CancellationToken cancellationToken = default)
    {
        using var response =
            await _httpClient.PostAsJsonAsync(
                new Uri(
                    _baseUri,
                    "router/identity/alias"),
                new
                {
                    device_alias = deviceAlias
                },
                cancellationToken);

        response.EnsureSuccessStatusCode();
    }

    public async Task ReverifyIdentityAsync(
        CancellationToken cancellationToken = default)
    {
        using var response =
            await _httpClient.PostAsync(
                new Uri(
                    _baseUri,
                    "router/identity/reverify"),
                content: null,
                cancellationToken);

        response.EnsureSuccessStatusCode();
    }

    public async Task BindIdentitySessionAsync(
        string sessionId,
        string operatorAlias,
        string targetDeviceId,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(sessionId);
        ArgumentException.ThrowIfNullOrWhiteSpace(operatorAlias);
        ArgumentException.ThrowIfNullOrWhiteSpace(targetDeviceId);

        using var response =
            await _httpClient.PostAsJsonAsync(
                new Uri(
                    _baseUri,
                    "router/identity/session/" +
                    Uri.EscapeDataString(sessionId.Trim()) +
                    "/bind"),
                new
                {
                    operator_alias = operatorAlias.Trim(),
                    target_device_id = targetDeviceId.Trim()
                },
                cancellationToken);

        response.EnsureSuccessStatusCode();
    }

    public async Task SwitchIdentitySessionAsync(
        string sessionId,
        string targetDeviceId,
        bool confirmation,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(sessionId);
        ArgumentException.ThrowIfNullOrWhiteSpace(targetDeviceId);

        using var response =
            await _httpClient.PostAsJsonAsync(
                new Uri(
                    _baseUri,
                    "router/identity/session/" +
                    Uri.EscapeDataString(sessionId.Trim()) +
                    "/switch"),
                new
                {
                    target_device_id = targetDeviceId.Trim(),
                    confirmation
                },
                cancellationToken);

        response.EnsureSuccessStatusCode();
    }

    public async Task RevokeIdentitySessionAsync(
        string sessionId,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(
            sessionId);

        using var response =
            await _httpClient.PostAsync(
                new Uri(
                    _baseUri,
                    "router/identity/session/" +
                    Uri.EscapeDataString(
                        sessionId.Trim()) +
                    "/unbind"),
                content: null,
                cancellationToken);

        response.EnsureSuccessStatusCode();
    }

    public async Task<IReadOnlyList<ManagerApprovalRow>> GetPluginAccessPendingAsync(
        CancellationToken cancellationToken = default)
    {
        using var document = await GetJsonAsync(
            "router/access/requests",
            cancellationToken);

        var rows = new List<ManagerApprovalRow>();
        if (!document.RootElement.TryGetProperty(
                "requests",
                out var requests) ||
            requests.ValueKind != JsonValueKind.Array)
        {
            return rows;
        }

        foreach (var row in requests.EnumerateArray())
        {
            var requestId =
                GetString(row, "request_id") ?? "";
            var plugin =
                GetString(row, "plugin_display_name") ??
                GetString(row, "plugin_audience") ??
                "Zagent";
            var operatorAlias =
                GetString(row, "operator_alias") ?? "";
            var sessionId =
                GetString(row, "session_id") ?? "";
            var deviceAlias =
                GetString(row, "device_alias") ?? "";
            var state =
                GetString(row, "state") ?? "PENDING";
            var createdAt =
                GetDouble(row, "created_at");

            rows.Add(
                new ManagerApprovalRow(
                    requestId,
                    "plugin_access",
                    state.ToLowerInvariant(),
                    operatorAlias,
                    createdAt,
                    $"Plugin {plugin} · Operator {operatorAlias} · Session {sessionId} · Device {deviceAlias}",
                    PrivilegeEscalation: false,
                    Source: "Plugin Access",
                    CanResolveLocally: true));
        }

        return rows;
    }

    public async Task<ManagerOperationResult> ResolvePluginAccessAsync(
        ManagerApprovalActionRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentException.ThrowIfNullOrWhiteSpace(
            request.RequestId);

        using var response =
            await _httpClient.PostAsJsonAsync(
                new Uri(
                    _baseUri,
                    "router/access/request/" +
                    Uri.EscapeDataString(
                        request.RequestId.Trim()) +
                    "/resolve"),
                new
                {
                    action = request.Action,
                    reason = request.Reason
                },
                cancellationToken);

        var body =
            await response.Content.ReadAsStringAsync(
                cancellationToken);

        if (!response.IsSuccessStatusCode)
        {
            return new ManagerOperationResult(
                false,
                "approval.resolve",
                request.RequestId,
                string.IsNullOrWhiteSpace(body)
                    ? "Plugin access approval failed."
                    : body);
        }

        using var document =
            JsonDocument.Parse(body);

        var root = document.RootElement;
        var state =
            GetString(root, "state") ??
            "unknown";
        var operatorAlias =
            GetString(root, "operator_alias") ??
            "";

        return new ManagerOperationResult(
            GetBool(root, "ok", true),
            "approval.resolve",
            request.RequestId,
            $"Plugin access {state.ToLowerInvariant()} for {operatorAlias}.");
    }

    public async Task<ManagerRouterSnapshot> SnapshotAsync(
        IReadOnlyList<ManagerAgentRow> agents,
        CancellationToken cancellationToken = default)
    {
        using var document = await GetJsonAsync(
            "router/status",
            cancellationToken);

        var root = document.RootElement;
        var rows = new List<ManagerRouterServiceRow>();

        if (root.TryGetProperty(
                "services",
                out var services) &&
            services.ValueKind == JsonValueKind.Array)
        {
            foreach (var service in services.EnumerateArray())
            {
                var id = GetString(service, "id") ?? "";
                var ns = GetString(service, "namespace") ?? "";
                var enabled = GetBool(
                    service,
                    "enabled",
                    true);
                var initialized = GetBool(
                    service,
                    "initialized",
                    false);
                var permanent = GetBool(
                    service,
                    "permanent",
                    false);
                var lastError =
                    GetString(service, "last_error") ?? "";

                var agent = FindAgent(
                    ns,
                    agents);

                var agentStatus = agent is null
                    ? initialized
                        ? "Reachable"
                        : enabled
                            ? "Unknown"
                            : "Disabled"
                    : agent.ProcessState;

                var mcpStatus = !enabled
                    ? "Disabled"
                    : initialized
                        ? "Connected"
                        : string.IsNullOrWhiteSpace(lastError)
                            ? "Disconnected"
                            : "Error";

                rows.Add(
                    new ManagerRouterServiceRow(
                        id,
                        permanent ? "Core" : "Additional",
                        GetString(service, "name") ?? id,
                        ns,
                        GetString(service, "url") ?? "",
                        agentStatus,
                        mcpStatus,
                        enabled ? "Enabled" : "Disabled",
                        GetInt(service, "tools") ?? 0,
                        permanent,
                        enabled,
                        lastError));
            }
        }

        var toolCount =
            GetInt(root, "tool_count") ??
            GetInt(root, "upstream_tool_count") ??
            rows.Sum(item => item.Tools);

        var ready = GetBool(
            root,
            "ok",
            true);

        return new ManagerRouterSnapshot(
            rows
                .OrderByDescending(item => item.Permanent)
                .ThenBy(
                    item => item.Name,
                    StringComparer.OrdinalIgnoreCase)
                .ToArray(),
            toolCount,
            ready ? "Ready" : "Offline",
            DateTimeOffset.UtcNow);
    }

    public async Task<ManagerOperationResult> ExecuteAsync(
        ManagerRouterActionRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);

        var action = (request.Action ?? "")
            .Trim()
            .ToLowerInvariant();

        if (action == "reload")
        {
            return await ReloadAsync(
                cancellationToken);
        }

        var serviceId =
            (request.ServiceId ?? "").Trim();

        if (string.IsNullOrWhiteSpace(serviceId))
        {
            return new ManagerOperationResult(
                false,
                "router." + action,
                "",
                "Select a Tunnel/MCP service first.");
        }

        return action switch
        {
            "enable" =>
                await SetRoutingAsync(
                    serviceId,
                    true,
                    cancellationToken),
            "disable" =>
                await SetRoutingAsync(
                    serviceId,
                    false,
                    cancellationToken),
            "reconnect" =>
                await ReconnectAsync(
                    serviceId,
                    cancellationToken),
            "test" =>
                await TestAsync(
                    serviceId,
                    cancellationToken),
            "tools" =>
                await ToolsAsync(
                    serviceId,
                    cancellationToken),
            _ => new ManagerOperationResult(
                false,
                "router." + action,
                serviceId,
                "Unknown Router action.")
        };
    }

    private async Task<ManagerOperationResult> SetRoutingAsync(
        string serviceId,
        bool enabled,
        CancellationToken cancellationToken)
    {
        await _configGate.WaitAsync(
            cancellationToken);

        try
        {
            var changed = false;

            foreach (var fileName in new[]
                     {
                         "core_services.json",
                         "additional_services.json"
                     })
            {
                var path = Path.Combine(
                    _configRoot,
                    fileName);

                if (!File.Exists(path))
                {
                    continue;
                }

                var raw = await File.ReadAllTextAsync(
                    path,
                    cancellationToken);
                var root = JsonNode.Parse(raw)?.AsObject();
                var services =
                    root?["services"]?.AsArray();

                if (root is null ||
                    services is null)
                {
                    continue;
                }

                foreach (var node in services)
                {
                    if (node is not JsonObject service)
                    {
                        continue;
                    }

                    var id =
                        service["id"]?.GetValue<string>() ?? "";

                    if (!string.Equals(
                            id,
                            serviceId,
                            StringComparison.Ordinal))
                    {
                        continue;
                    }

                    service["enabled"] = enabled;

                    var temporary =
                        path + ".manager.tmp";

                    await File.WriteAllTextAsync(
                        temporary,
                        root.ToJsonString(
                            new JsonSerializerOptions
                            {
                                WriteIndented = true
                            }) + Environment.NewLine,
                        cancellationToken);

                    File.Move(
                        temporary,
                        path,
                        overwrite: true);

                    changed = true;
                    break;
                }

                if (changed)
                {
                    break;
                }
            }

            if (!changed)
            {
                return new ManagerOperationResult(
                    false,
                    enabled
                        ? "router.enable"
                        : "router.disable",
                    serviceId,
                    "Service was not found in Tunnel configuration.");
            }
        }
        finally
        {
            _configGate.Release();
        }

        using var refresh =
            await PostJsonAsync(
                "router/refresh",
                cancellationToken);

        return new ManagerOperationResult(
            true,
            enabled
                ? "router.enable"
                : "router.disable",
            serviceId,
            enabled
                ? "Routing enabled and service list reloaded."
                : "Routing disabled and service list reloaded.");
    }

    private async Task<ManagerOperationResult> ReconnectAsync(
        string serviceId,
        CancellationToken cancellationToken)
    {
        using var document =
            await PostJsonAsync(
                "router/service/" +
                Uri.EscapeDataString(serviceId) +
                "/refresh",
                cancellationToken);

        var root = document.RootElement;
        var ok = GetBool(
            root,
            "ok",
            true);

        return new ManagerOperationResult(
            ok,
            "router.reconnect",
            serviceId,
            ok
                ? "MCP service reconnected."
                : GetString(root, "error") ??
                    "Reconnect failed.");
    }

    private async Task<ManagerOperationResult> TestAsync(
        string serviceId,
        CancellationToken cancellationToken)
    {
        using var document = await GetJsonAsync(
            "router/status",
            cancellationToken);

        if (!TryFindService(
                document.RootElement,
                serviceId,
                out var service))
        {
            return new ManagerOperationResult(
                false,
                "router.test",
                serviceId,
                "Service was not found.");
        }

        var rawUrl =
            GetString(service, "url") ?? "";

        if (!Uri.TryCreate(
                rawUrl,
                UriKind.Absolute,
                out var uri))
        {
            return new ManagerOperationResult(
                false,
                "router.test",
                serviceId,
                "Service URL is invalid.");
        }

        try
        {
            using var tcp = new TcpClient();
            using var timeout =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);

            timeout.CancelAfter(
                TimeSpan.FromMilliseconds(1200));

            await tcp.ConnectAsync(
                uri.Host,
                uri.Port,
                timeout.Token);

            return new ManagerOperationResult(
                true,
                "router.test",
                serviceId,
                "Connection PASS (" +
                uri.Host +
                ":" +
                uri.Port +
                ").");
        }
        catch (Exception ex)
        {
            return new ManagerOperationResult(
                false,
                "router.test",
                serviceId,
                "Connection FAILED: " +
                ex.Message);
        }
    }

    private async Task<ManagerOperationResult> ToolsAsync(
        string serviceId,
        CancellationToken cancellationToken)
    {
        using var document = await GetJsonAsync(
            "router/service/" +
            Uri.EscapeDataString(serviceId) +
            "/tools",
            cancellationToken);

        var names = new List<string>();

        if (document.RootElement.TryGetProperty(
                "tools",
                out var tools) &&
            tools.ValueKind == JsonValueKind.Array)
        {
            foreach (var tool in tools.EnumerateArray())
            {
                var name =
                    GetString(tool, "name");

                if (!string.IsNullOrWhiteSpace(name))
                {
                    names.Add(name);
                }
            }
        }

        return new ManagerOperationResult(
            true,
            "router.tools",
            serviceId,
            names.Count == 0
                ? "No routed tools are currently exposed."
                : string.Join(
                    Environment.NewLine,
                    names.OrderBy(
                        item => item,
                        StringComparer.OrdinalIgnoreCase)));
    }

    private async Task<ManagerOperationResult> ReloadAsync(
        CancellationToken cancellationToken)
    {
        using var document =
            await PostJsonAsync(
                "router/refresh",
                cancellationToken);

        return new ManagerOperationResult(
            true,
            "router.reload",
            "tunnel",
            "Tunnel service list reloaded.");
    }

    private async Task<JsonDocument> GetJsonAsync(
        string relative,
        CancellationToken cancellationToken)
    {
        using var response =
            await _httpClient.GetAsync(
                new Uri(
                    _baseUri,
                    relative),
                cancellationToken);

        var body =
            await response.Content.ReadAsStringAsync(
                cancellationToken);

        response.EnsureSuccessStatusCode();

        return JsonDocument.Parse(body);
    }

    private async Task<JsonDocument> PostJsonAsync(
        string relative,
        CancellationToken cancellationToken)
    {
        using var response =
            await _httpClient.PostAsync(
                new Uri(
                    _baseUri,
                    relative),
                content: null,
                cancellationToken);

        var body =
            await response.Content.ReadAsStringAsync(
                cancellationToken);

        response.EnsureSuccessStatusCode();

        return JsonDocument.Parse(body);
    }

    private static ManagerAgentRow? FindAgent(
        string serviceNamespace,
        IReadOnlyList<ManagerAgentRow> agents)
    {
        var agentId =
            serviceNamespace.ToLowerInvariant() switch
            {
                "files" => "files.service",
                "web" => "web.service",
                "arcmap" => "arcmap.service",
                _ => ""
            };

        return string.IsNullOrWhiteSpace(agentId)
            ? null
            : agents.FirstOrDefault(
                item =>
                    item.AgentId == agentId);
    }

    private static bool TryFindService(
        JsonElement root,
        string serviceId,
        out JsonElement service)
    {
        service = default;

        if (!root.TryGetProperty(
                "services",
                out var services) ||
            services.ValueKind != JsonValueKind.Array)
        {
            return false;
        }

        foreach (var item in services.EnumerateArray())
        {
            if (string.Equals(
                    GetString(item, "id"),
                    serviceId,
                    StringComparison.Ordinal))
            {
                service = item;
                return true;
            }
        }

        return false;
    }

    private static string? GetString(
        JsonElement element,
        string name)
    {
        if (!element.TryGetProperty(
                name,
                out var value))
        {
            return null;
        }

        return value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : value.ToString();
    }

    private static double? GetDouble(
        JsonElement element,
        string name)
    {
        if (!element.TryGetProperty(
                name,
                out var value))
        {
            return null;
        }

        return value.ValueKind == JsonValueKind.Number &&
               value.TryGetDouble(out var number)
            ? number
            : null;
    }

    private static bool GetBool(
        JsonElement element,
        string name,
        bool fallback)
    {
        if (!element.TryGetProperty(
                name,
                out var value))
        {
            return fallback;
        }

        return value.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => fallback
        };
    }

    private static int? GetInt(
        JsonElement element,
        string name)
    {
        if (!element.TryGetProperty(
                name,
                out var value))
        {
            return null;
        }

        return value.TryGetInt32(
            out var number)
            ? number
            : null;
    }

    private static DateTimeOffset? FromUnixSeconds(
        JsonElement element,
        string name)
    {
        if (!element.TryGetProperty(
                name,
                out var value) ||
            !value.TryGetDouble(
                out var seconds) ||
            seconds <= 0)
        {
            return null;
        }

        try
        {
            return DateTimeOffset
                .FromUnixTimeMilliseconds(
                    checked((long)(seconds * 1000)));
        }
        catch
        {
            return null;
        }
    }
}
