using System.Text.Json;

namespace Zagent.Shared.Installation;

/// <summary>Local installation identity, distinct from the bound operator/device alias.</summary>
public static class ZagentInstallationProfile
{
    public const string Master = "Master";
    public const string User = "User";

    public static string Normalize(string? role) =>
        (role ?? "").Trim().ToLowerInvariant() switch
        {
            "" or "master" or "ikbal" => Master,
            "user" or "harry" => User,
            _ => throw new ArgumentException("Installation role must be Master or User.", nameof(role))
        };

    public static string FromAudience(string? audience) =>
        string.Equals(audience?.Trim(), "tryplugin", StringComparison.OrdinalIgnoreCase)
            ? User : Master;

    public static string DisplayName(string? role) => "Zagent " + Normalize(role);
    public static string FolderName(string? role) => "Zagent " + Normalize(role);
    public static string PipeName(string? role) => "Zagent.Manager." + Normalize(role) + ".v1";
    public static string SupervisorLeaseName(string? role) =>
        @"Local\Zagent." + Normalize(role) + ".Supervisor.v3";

    public static string InferRole(string installRoot)
    {
        var root = Path.GetFullPath(installRoot);
        var configPath = Path.Combine(root, "data", "config", "zagent.machine.json");
        if (File.Exists(configPath))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(configPath));
                if ((doc.RootElement.TryGetProperty("installation_role", out var value) ||
                     doc.RootElement.TryGetProperty("InstallationRole", out value)) &&
                    value.ValueKind == JsonValueKind.String)
                    return Normalize(value.GetString());
            }
            catch (JsonException) { }
        }
        var pluginPath = Path.Combine(root, "data", "config", "plugin-access.json");
        if (File.Exists(pluginPath))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(pluginPath));
                if (doc.RootElement.TryGetProperty("audience", out var value))
                    return FromAudience(value.GetString());
            }
            catch (JsonException) { }
        }
        return Path.GetFileName(Path.TrimEndingDirectorySeparator(root))
            .Equals(FolderName(User), StringComparison.OrdinalIgnoreCase) ? User : Master;
    }
}
