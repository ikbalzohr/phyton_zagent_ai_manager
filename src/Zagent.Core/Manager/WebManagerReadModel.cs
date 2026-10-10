using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using Zagent.Shared.Manager;

namespace Zagent.Core.Manager;

public sealed record WebBridgeManagerStatus(
    bool Connected,
    bool Authorized,
    bool ProfileBound,
    bool NativeAttached,
    bool PairedOriginKnown,
    bool ExtensionHeartbeatDetected,
    int ObservedExtensionCount,
    string ExpectedBridgeEndpoint,
    string BridgeEnvironment,
    string ProfileDirectory,
    string ProfileName);

public sealed class WebManagerReadModel
{
    private readonly HttpClient _httpClient;
    private readonly Uri _mcpUri;

    public WebManagerReadModel(
        Uri mcpUri,
        HttpClient? httpClient = null)
    {
        ArgumentNullException.ThrowIfNull(mcpUri);

        if (!mcpUri.IsLoopback)
        {
            throw new ArgumentException(
                "Web Manager endpoint must be loopback.",
                nameof(mcpUri));
        }

        _mcpUri = mcpUri;
        _httpClient = httpClient ?? new HttpClient(
            new SocketsHttpHandler
            {
                UseProxy = false
            })
        {
            Timeout = Timeout.InfiniteTimeSpan
        };
    }

    public async Task<WebBridgeManagerStatus> GetBridgeStatusAsync(
        CancellationToken cancellationToken = default)
    {
        using var status = await CallToolForJsonAsync(
            "web_bridge_status",
            arguments: null,
            cancellationToken);

        var root = status.RootElement;
        var profileDirectory = "";
        var profileName = "";

        if (root.TryGetProperty(
                "profileBinding",
                out var binding) &&
            binding.ValueKind == JsonValueKind.Object)
        {
            profileDirectory =
                String(binding, "directory") ?? "";
            profileName =
                String(binding, "name") ?? "";
        }

        var observedExtensionCount =
            root.TryGetProperty(
                "observedExtensionProfiles",
                out var observedProfiles) &&
            observedProfiles.ValueKind == JsonValueKind.Array
                ? observedProfiles.GetArrayLength()
                : 0;

        return new WebBridgeManagerStatus(
            Connected: Bool(root, "connected"),
            Authorized: Bool(root, "authorized"),
            ProfileBound: Bool(root, "profileBound"),
            NativeAttached: Bool(root, "nativeAttached"),
            PairedOriginKnown: Bool(root, "pairedOriginKnown"),
            ExtensionHeartbeatDetected:
                Bool(root, "extensionHeartbeatDetected") ||
                observedExtensionCount > 0,
            ObservedExtensionCount: observedExtensionCount,
            ExpectedBridgeEndpoint:
                String(root, "expectedBridgeEndpoint") ?? "",
            BridgeEnvironment:
                String(root, "bridgeEnvironment") ?? "",
            ProfileDirectory: profileDirectory,
            ProfileName: profileName);
    }

    public async Task<string> PairOrReconnectAsync(
        CancellationToken cancellationToken = default)
    {
        var status =
            await GetBridgeStatusAsync(cancellationToken);

        var tool = status.ProfileBound
            ? "web_bridge_reconnect"
            : "web_bridge_pair";

        object? arguments = status.ProfileBound
            ? null
            : new
            {
                expiresInSeconds = 30
            };

        using var result = await CallToolForJsonAsync(
            tool,
            arguments,
            cancellationToken,
            TimeSpan.FromSeconds(35));

        var root = result.RootElement;
        return String(root, "outcome") ??
            (Bool(root, "connected")
                ? "connected"
                : "request created");
    }

    public async Task<string> RequestAuthorizationAsync(
        CancellationToken cancellationToken = default)
    {
        using var result = await CallToolForJsonAsync(
            "web_bridge_authorize_request",
            new
            {
                scope = "window",
                seconds = 900,
                allowNavigation = true,
                allowNewTabs = true,
                expiresInSeconds = 30
            },
            cancellationToken,
            TimeSpan.FromSeconds(35));

        var root = result.RootElement;
        return String(root, "outcome") ??
            (Bool(root, "authorized")
                ? "approved"
                : "request created");
    }

    public async Task<(
        IReadOnlyList<ManagerApprovalRow> Requests,
        bool Connected,
        bool Authorized)> GetPendingRequestsAsync(
        CancellationToken cancellationToken = default)
    {
        using var status = await CallToolForJsonAsync(
            "web_bridge_pair_status",
            arguments: null,
            cancellationToken);

        var root = status.RootElement;
        var connected = Bool(root, "connected");
        var authorized = Bool(root, "authorized");
        var rows = new List<ManagerApprovalRow>();

        if (root.TryGetProperty(
                "pairingPending",
                out var pair) &&
            pair.ValueKind == JsonValueKind.Object)
        {
            var id = String(pair, "id") ?? "";
            var profile = "";

            if (pair.TryGetProperty(
                    "profile",
                    out var profileElement))
            {
                profile = profileElement.ValueKind ==
                    JsonValueKind.Object
                    ? String(
                        profileElement,
                        "directory") ??
                        profileElement.ToString()
                    : profileElement.ToString();
            }

            rows.Add(
                new ManagerApprovalRow(
                    id,
                    "web_pairing",
                    "pending_user_action",
                    "Zagent Web",
                    null,
                    string.IsNullOrWhiteSpace(profile)
                        ? "Pair the requested Chrome profile in Zagent Chrome Bridge."
                        : $"Pair Chrome profile '{profile}' in Zagent Chrome Bridge.",
                    false,
                    "Web Chrome Bridge",
                    false));
        }

        if (root.TryGetProperty(
                "authorizationPending",
                out var authorization) &&
            authorization.ValueKind == JsonValueKind.Object)
        {
            var id = String(
                authorization,
                "id") ?? "";
            var scope = String(
                authorization,
                "scope") ?? "window";
            var seconds =
                Int(
                    authorization,
                    "seconds") ??
                0;

            rows.Add(
                new ManagerApprovalRow(
                    id,
                    "web_authorization",
                    "pending_user_action",
                    "Zagent Web",
                    null,
                    seconds > 0
                        ? $"Approve Chrome automation scope '{scope}' for {seconds} seconds in Zagent Chrome Bridge."
                        : $"Approve Chrome automation scope '{scope}' in Zagent Chrome Bridge.",
                    true,
                    "Web Chrome Bridge",
                    false));
        }

        return (
            rows,
            connected,
            authorized);
    }

    private async Task<JsonDocument> CallToolForJsonAsync(
        string toolName,
        object? arguments,
        CancellationToken cancellationToken,
        TimeSpan? timeout = null)
    {
        var rpc = JsonSerializer.Serialize(
            new
            {
                jsonrpc = "2.0",
                id = Guid.NewGuid().ToString("N"),
                method = "tools/call",
                @params = new
                {
                    name = toolName,
                    arguments = arguments ?? new { }
                }
            });

        using var request = new HttpRequestMessage(
            HttpMethod.Post,
            _mcpUri);

        request.Content = new StringContent(
            rpc,
            Encoding.UTF8,
            "application/json");

        request.Headers.Accept.Add(
            new MediaTypeWithQualityHeaderValue(
                "application/json"));
        request.Headers.Accept.Add(
            new MediaTypeWithQualityHeaderValue(
                "text/event-stream"));
        request.Headers.TryAddWithoutValidation(
            "MCP-Protocol-Version",
            "2025-06-18");

        using var timeoutSource =
            CancellationTokenSource.CreateLinkedTokenSource(
                cancellationToken);
        timeoutSource.CancelAfter(
            timeout ?? TimeSpan.FromSeconds(5));

        using var response = await _httpClient.SendAsync(
            request,
            timeoutSource.Token);

        var body = await response.Content.ReadAsStringAsync(
            timeoutSource.Token);

        response.EnsureSuccessStatusCode();

        using var rpcDocument = ParseRpcBody(body);
        var result = rpcDocument.RootElement.GetProperty(
            "result");

        if (result.TryGetProperty(
                "isError",
                out var isError) &&
            isError.ValueKind == JsonValueKind.True)
        {
            throw new InvalidOperationException(
                result.ToString());
        }

        if (result.TryGetProperty(
                "structuredContent",
                out var structured) &&
            structured.ValueKind is not
                JsonValueKind.Null and not
                JsonValueKind.Undefined)
        {
            if (structured.ValueKind ==
                    JsonValueKind.Object &&
                structured.TryGetProperty(
                    "result",
                    out var wrapped))
            {
                if (wrapped.ValueKind ==
                        JsonValueKind.String)
                {
                    var raw = wrapped.GetString();
                    if (!string.IsNullOrWhiteSpace(raw))
                    {
                        return JsonDocument.Parse(raw);
                    }
                }

                if (wrapped.ValueKind is
                    JsonValueKind.Object or
                    JsonValueKind.Array)
                {
                    return JsonDocument.Parse(
                        wrapped.ToString());
                }
            }

            if (structured.ValueKind is
                JsonValueKind.Object or
                JsonValueKind.Array)
            {
                return JsonDocument.Parse(
                    structured.ToString());
            }
        }

        if (result.TryGetProperty(
                "content",
                out var content) &&
            content.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in content.EnumerateArray())
            {
                if (item.TryGetProperty(
                        "text",
                        out var text) &&
                    text.ValueKind == JsonValueKind.String)
                {
                    var raw = text.GetString();
                    if (!string.IsNullOrWhiteSpace(raw))
                    {
                        return JsonDocument.Parse(raw);
                    }
                }
            }
        }

        throw new InvalidDataException(
            $"Web MCP tool '{toolName}' returned no JSON payload.");
    }

    private static JsonDocument ParseRpcBody(
        string body)
    {
        if (body.TrimStart().StartsWith(
                "event:",
                StringComparison.Ordinal))
        {
            var payloads = body
                .Split(
                    new[] { "\r\n", "\n" },
                    StringSplitOptions.None)
                .Where(
                    line =>
                        line.StartsWith(
                            "data:",
                            StringComparison.Ordinal))
                .Select(line => line[5..].Trim())
                .Where(
                    line =>
                        !string.IsNullOrWhiteSpace(line) &&
                        line != "[DONE]")
                .ToArray();

            if (payloads.Length == 0)
            {
                throw new InvalidDataException(
                    "Web MCP SSE response had no data payload.");
            }

            return JsonDocument.Parse(payloads[^1]);
        }

        return JsonDocument.Parse(body);
    }

    private static string? String(
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

    private static bool Bool(
        JsonElement element,
        string name)
    {
        if (!element.TryGetProperty(
                name,
                out var value))
        {
            return false;
        }

        return value.ValueKind == JsonValueKind.True;
    }

    private static int? Int(
        JsonElement element,
        string name)
    {
        if (!element.TryGetProperty(
                name,
                out var value))
        {
            return null;
        }

        return value.TryGetInt32(out var number)
            ? number
            : null;
    }
}