using System.Text.Json;
using Zagent.Shared.Manager;

namespace Zagent.Core.Manager;

public sealed class ManagerVisualReadModel
{
    private readonly string _root;
    private readonly string _latestPath;

    public ManagerVisualReadModel(string visualRoot)
    {
        _root = Path.GetFullPath(
            visualRoot ??
            throw new ArgumentNullException(nameof(visualRoot)));
        _latestPath = Path.Combine(_root, "latest.json");
    }

    public async Task<ManagerVisualSnapshot> GetLatestAsync(
        CancellationToken cancellationToken = default)
    {
        if (!File.Exists(_latestPath))
        {
            return Empty();
        }

        try
        {
            await using var stream = File.Open(
                _latestPath,
                FileMode.Open,
                FileAccess.Read,
                FileShare.ReadWrite | FileShare.Delete);

            var manifest = await JsonSerializer.DeserializeAsync<VisualManifest>(
                stream,
                new JsonSerializerOptions
                {
                    PropertyNameCaseInsensitive = true
                },
                cancellationToken);

            if (manifest is null ||
                string.IsNullOrWhiteSpace(manifest.ArtifactPath))
            {
                return Empty("Visual manifest is empty.");
            }

            var artifactPath = Path.GetFullPath(manifest.ArtifactPath);
            if (!IsUnderRoot(artifactPath))
            {
                return Empty(
                    "Visual manifest points outside the shared visual root.");
            }

            if (!File.Exists(artifactPath))
            {
                return Empty(
                    "Latest visual artifact file no longer exists.");
            }

            return new ManagerVisualSnapshot(
                true,
                manifest.Id,
                manifest.Source,
                manifest.Kind,
                manifest.Label,
                ParseTimestamp(manifest.CapturedAt),
                artifactPath,
                manifest.MimeType,
                manifest.Bytes,
                manifest.Provenance?.DeviceId,
                manifest.Provenance?.DeviceName,
                manifest.Provenance?.SessionId,
                null);
        }
        catch (Exception ex)
        {
            return Empty(ex.Message);
        }
    }

    private bool IsUnderRoot(string candidate)
    {
        var root = _root.TrimEnd(
            Path.DirectorySeparatorChar,
            Path.AltDirectorySeparatorChar) +
            Path.DirectorySeparatorChar;

        return candidate.StartsWith(
            root,
            OperatingSystem.IsWindows()
                ? StringComparison.OrdinalIgnoreCase
                : StringComparison.Ordinal);
    }

    private static DateTimeOffset? ParseTimestamp(string? value) =>
        DateTimeOffset.TryParse(value, out var parsed)
            ? parsed
            : null;

    private static ManagerVisualSnapshot Empty(string? error = null) =>
        new(
            false,
            null,
            null,
            null,
            null,
            null,
            null,
            null,
            0,
            null,
            null,
            null,
            error);

    private sealed record VisualManifest(
        string? Id,
        string? Source,
        string? Kind,
        string? Label,
        string? CapturedAt,
        string? ArtifactPath,
        string? MimeType,
        long Bytes,
        VisualProvenance? Provenance);

    private sealed record VisualProvenance(
        string? DeviceId,
        string? DeviceName,
        string? SessionId);
}
